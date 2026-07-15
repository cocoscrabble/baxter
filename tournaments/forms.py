from django import forms

from users.models import User

from django.forms import formset_factory

from .models import Entrant, Pairing, Player, ResultSlip, RoundPairings, Tournament
from .pairing.round_pairing import STRATEGY_TYPES


def clean_multiline_text(text):
    """Parse multiline text into a list of unique, non-empty lines."""
    if not text.strip():
        return []

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    return unique_lines


class TournamentForm(forms.ModelForm):
    """Form for creating and editing tournaments."""

    editor_usernames = forms.CharField(
        label="Additional editors",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Enter usernames, one per line.",
    )

    class Meta:
        model = Tournament
        fields = ["name", "location", "start_date"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Populate editor_usernames for existing tournament
            editor_names = self.instance.editors.exclude(
                pk=self.instance.owner.pk
            ).values_list("username", flat=True)
            self.fields["editor_usernames"].initial = "\n".join(editor_names)

    def clean_editor_usernames(self):
        """Validate that all usernames exist."""
        usernames = clean_multiline_text(self.cleaned_data.get("editor_usernames", ""))
        valid_users = []
        invalid_usernames = []

        for username in usernames:
            try:
                user = User.objects.get(username=username)
                valid_users.append(user)
            except User.DoesNotExist:
                invalid_usernames.append(username)

        if invalid_usernames:
            raise forms.ValidationError(
                f"Users not found: {', '.join(invalid_usernames)}"
            )

        return valid_users

    def save(self, commit=True):
        tournament = super().save(commit=commit)
        if commit:
            editor_users = self.cleaned_data.get("editor_usernames", [])
            all_editors = set(editor_users)
            all_editors.add(tournament.owner)
            tournament.editors.set(all_editors)
        return tournament


class ResultSlipForm(forms.Form):
    """Form for entering game results via pairing selection."""

    round = forms.IntegerField(widget=forms.Select())
    pairing = forms.IntegerField(widget=forms.Select())
    winner = forms.IntegerField(widget=forms.Select())
    winner_score = forms.IntegerField()
    loser_score = forms.IntegerField(label="Opponent score")
    # Not stored: a gate forcing the submitter to confirm the opponent agreed the
    # result before it can be saved. Required, so an unchecked box fails validation.
    verified_by_opponent = forms.BooleanField(
        required=True,
        label="Verified by opponent",
        error_messages={"required": "The result must be verified by the opponent before saving."},
    )

    def __init__(self, *args, division=None, pairings_by_round=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._division = division
        # When set, the form edits this existing ResultSlip in place instead of
        # creating a new one.
        self.instance = instance
        # pairings_by_round: {round_num: [(pairing_pk, first_pk, first_name, second_pk, second_name), ...]}
        self._pairings_by_round = pairings_by_round or {}
        self._pairing_lookup = {}  # pk -> Pairing data

        round_choices = [("", "---")]
        for r in sorted(self._pairings_by_round.keys()):
            round_choices.append((r, str(r)))
        self.fields["round"].widget = forms.Select(choices=round_choices)

        # Build flat pairing choices and lookup.
        pairing_choices = [("", "---")]
        # (pk, label, round) per pairing, for rendering the pairing options.
        # The template shows all of them and filters to the selected round
        # client-side (data-show), so a JS failure just leaves the full list.
        pairing_options = []
        # entrant pk -> (name, [pairing pks the entrant plays in]). Drives the
        # winner dropdown, which is filtered client-side to the selected pairing.
        winner_options = {}
        for r, pairing_list in sorted(self._pairings_by_round.items()):
            for p_pk, first_pk, first_name, second_pk, second_name in pairing_list:
                label = f"{first_name} vs. {second_name}"
                pairing_choices.append((p_pk, label))
                pairing_options.append((p_pk, label, r))
                self._pairing_lookup[p_pk] = (
                    first_pk,
                    first_name,
                    second_pk,
                    second_name,
                    r,
                )
                for ent_pk, ent_name in ((first_pk, first_name), (second_pk, second_name)):
                    entry = winner_options.setdefault(ent_pk, (ent_name, []))
                    entry[1].append(p_pk)

        self.pairing_options = pairing_options
        self.fields["pairing"].widget = forms.Select(choices=pairing_choices)
        # (pk, name, [pairing pks]) per entrant, for rendering the winner options.
        self.winner_options = [
            (ent_pk, name, pairings) for ent_pk, (name, pairings) in winner_options.items()
        ]
        # The winner field accepts any entrant pk; the Select widget is only for
        # non-JS fallback (the template renders its own filtered options).
        self.fields["winner"].widget = forms.Select(
            choices=[("", "---")] + [(pk, name) for pk, name, _ in self.winner_options]
        )

        for field_name, field in self.fields.items():
            field.widget.attrs["data-bind"] = field_name

        # Picking a different pairing clears any winner chosen for the previous
        # one; the winner dropdown is restricted client-side to the two players
        # in the selected pairing (see _resultslip_form.html).
        self.fields["pairing"].widget.attrs["data-on:change"] = "$winner = ''"
        # Changing the round clears the pairing (and winner): the pairing
        # dropdown is filtered to the selected round, so a pairing picked for a
        # different round must not linger. JS-only refinement — without it the
        # server-rendered options still all submit correctly.
        self.fields["round"].widget.attrs["data-on:change"] = "$pairing = ''; $winner = ''"

    def clean_pairing(self):
        pairing_pk = self.cleaned_data.get("pairing")
        if not pairing_pk:
            raise forms.ValidationError("Please select a pairing.")
        if pairing_pk not in self._pairing_lookup:
            raise forms.ValidationError("Invalid pairing selection.")
        # Verify no result already exists for this pairing.
        try:
            pairing_obj = Pairing.objects.get(pk=pairing_pk)
        except Pairing.DoesNotExist:
            raise forms.ValidationError("Pairing not found.")
        existing = pairing_obj.result if hasattr(pairing_obj, "result") else None
        if existing is not None and not (
            self.instance is not None and existing.pk == self.instance.pk
        ):
            raise forms.ValidationError("This pairing already has a result.")
        return pairing_obj

    def clean_winner(self):
        winner_pk = self.cleaned_data.get("winner")
        if not winner_pk:
            raise forms.ValidationError("Please select a winner.")
        try:
            return Entrant.objects.get(pk=winner_pk)
        except Entrant.DoesNotExist:
            raise forms.ValidationError("Winner not found.")

    def clean(self):
        cleaned_data = super().clean()
        pairing = cleaned_data.get("pairing")
        winner = cleaned_data.get("winner")
        if pairing and winner:
            valid_ids = {pairing.first_id, pairing.second_id}
            if winner.pk not in valid_ids:
                raise forms.ValidationError(
                    "Winner must be one of the players in the pairing."
                )
        winner_score = cleaned_data.get("winner_score")
        loser_score = cleaned_data.get("loser_score")
        if (
            winner_score is not None
            and loser_score is not None
            and winner_score < loser_score
        ):
            raise forms.ValidationError(
                "Winner score must be greater than or equal to the opponent score."
            )
        return cleaned_data

    def save(self):
        pairing = self.cleaned_data["pairing"]
        winner = self.cleaned_data["winner"]
        loser = pairing.first if winner.pk == pairing.second_id else pairing.second
        # The pairing records who goes first (the `first` entrant starts), so
        # derive winner_started from it rather than asking the submitter.
        winner_started = winner.pk == pairing.first_id
        rp = pairing.round_pairings
        fields = dict(
            division=rp.division,
            round=rp.round,
            pairing=pairing,
            winner=winner,
            winner_score=self.cleaned_data["winner_score"],
            loser=loser,
            loser_score=self.cleaned_data["loser_score"],
            winner_started=winner_started,
        )
        if self.instance is not None:
            for name, value in fields.items():
                setattr(self.instance, name, value)
            self.instance.save()
            return self.instance
        return ResultSlip.objects.create(**fields)


class FakeTournamentForm(forms.Form):
    """Parameters for generating a fully-simulated test tournament."""

    name = forms.CharField(max_length=200, label="Tournament name")
    num_players = forms.IntegerField(min_value=2, label="Number of players")
    num_rounds = forms.IntegerField(min_value=1, label="Number of rounds")

    def clean_num_players(self):
        num = self.cleaned_data["num_players"]
        # Odd fields are fine now — the pairing engine adds a bye automatically.
        # Provisional players are excluded from fake tournaments, so only count
        # the eligible roster here (matches create_fake_tournament).
        available = Player.objects.filter(is_provisional=False).count()
        if num > available:
            raise forms.ValidationError(
                f"Only {available} non-provisional player(s) available to choose from."
            )
        return num


class RoundCountForm(forms.Form):
    num_rounds = forms.IntegerField(min_value=1, label="Number of rounds")


class RoundPairingForm(forms.Form):
    round = forms.IntegerField(widget=forms.HiddenInput)
    pairing_type = forms.ChoiceField(
        choices=[(s, s) for s in STRATEGY_TYPES],
        label="Pairing type",
    )
    start_round = forms.IntegerField(min_value=0, label="Based on round")

    def clean(self):
        cleaned_data = super().clean()
        round_num = cleaned_data.get("round")
        start_round = cleaned_data.get("start_round")
        if round_num is not None and start_round is not None:
            if start_round >= round_num:
                raise forms.ValidationError(
                    f"Based on round must be less than {round_num}."
                )
        return cleaned_data


RoundPairingFormSet = formset_factory(RoundPairingForm, extra=0)
