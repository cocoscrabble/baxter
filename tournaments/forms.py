from django import forms

from users.models import User

from django.forms import formset_factory

from .models import Entrant, ResultSlip, Tournament
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


class ResultSlipForm(forms.ModelForm):
    """Form for entering game results."""

    winner = forms.CharField(widget=forms.TextInput(attrs={"list": "players-datalist"}))
    loser = forms.CharField(label="Opponent", widget=forms.TextInput(attrs={"list": "players-datalist"}))

    class Meta:
        model = ResultSlip
        fields = ["round", "winner", "winner_score", "loser", "loser_score", "winner_started"]

    def __init__(self, *args, division=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._division = division or (self.instance.division if self.instance.pk else None)
        if self.instance.pk:
            if self.instance.winner:
                self.fields["winner"].initial = self.instance.winner.player.name
            if self.instance.loser:
                self.fields["loser"].initial = self.instance.loser.player.name
        for field_name, field in self.fields.items():
            field.widget.attrs["data-bind"] = field_name

    def _get_entrant(self, name):
        if not self._division:
            raise forms.ValidationError("Division not set.")
        try:
            return Entrant.objects.select_related("player").get(
                division=self._division, player__name=name
            )
        except Entrant.DoesNotExist:
            raise forms.ValidationError(f"Player '{name}' not found in this division.")

    def clean_winner(self):
        return self._get_entrant(self.cleaned_data.get("winner", ""))

    def clean_loser(self):
        return self._get_entrant(self.cleaned_data.get("loser", ""))

    def clean(self):
        cleaned_data = super().clean()
        winner = cleaned_data.get("winner")
        loser = cleaned_data.get("loser")
        if winner and loser and winner == loser:
            raise forms.ValidationError("Winner and opponent must be different players.")
        return cleaned_data


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


