from django import forms

from users.models import User

from django.forms import formset_factory

from .models import Entrant, Pairing, ResultSlip, RoundPairings, Tournament
from .pairing.pair import STRATEGY_TYPES


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
    winner_started = forms.BooleanField(required=False)

    def __init__(self, *args, division=None, pairings_by_round=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._division = division
        # pairings_by_round: {round_num: [(pairing_pk, first_pk, first_name, second_pk, second_name), ...]}
        self._pairings_by_round = pairings_by_round or {}
        self._pairing_lookup = {}  # pk -> Pairing data

        round_choices = [("", "---")]
        for r in sorted(self._pairings_by_round.keys()):
            round_choices.append((r, f"Round {r}"))
        self.fields["round"].widget = forms.Select(choices=round_choices)

        # Build flat pairing choices and lookup.
        pairing_choices = [("", "---")]
        winner_choices = [("", "---")]
        for r, pairing_list in sorted(self._pairings_by_round.items()):
            for p_pk, first_pk, first_name, second_pk, second_name in pairing_list:
                label = f"{first_name} vs. {second_name}"
                pairing_choices.append((p_pk, label))
                self._pairing_lookup[p_pk] = (
                    first_pk,
                    first_name,
                    second_pk,
                    second_name,
                    r,
                )
                winner_choices.append((first_pk, first_name))
                winner_choices.append((second_pk, second_name))

        self.fields["pairing"].widget = forms.Select(choices=pairing_choices)
        # Deduplicate winner choices.
        seen = set()
        unique_winner_choices = [("", "---")]
        for val, label in winner_choices[1:]:
            if val not in seen:
                seen.add(val)
                unique_winner_choices.append((val, label))
        self.fields["winner"].widget = forms.Select(choices=unique_winner_choices)

        for field_name, field in self.fields.items():
            field.widget.attrs["data-bind"] = field_name

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
        if hasattr(pairing_obj, "result") and pairing_obj.result is not None:
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
        return cleaned_data

    def save(self):
        pairing = self.cleaned_data["pairing"]
        winner = self.cleaned_data["winner"]
        loser = pairing.first if winner.pk == pairing.second_id else pairing.second
        rp = pairing.round_pairings
        return ResultSlip.objects.create(
            division=rp.division,
            round=rp.round,
            pairing=pairing,
            winner=winner,
            winner_score=self.cleaned_data["winner_score"],
            loser=loser,
            loser_score=self.cleaned_data["loser_score"],
            winner_started=self.cleaned_data["winner_started"],
        )


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
