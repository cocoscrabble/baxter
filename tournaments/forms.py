from django import forms

from users.models import User

from .models import Division, Entrant, ResultSlip, Tournament


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

    division_names = forms.CharField(
        label="Divisions",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Enter division names, one per line.",
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

            # Populate division_names for existing tournament
            divisions = self.instance.divisions.values_list("name", flat=True)
            self.fields["division_names"].initial = "\n".join(divisions)

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

    def clean_division_names(self):
        """Parse division names."""
        return clean_multiline_text(self.cleaned_data.get("division_names", ""))

    def save(self, commit=True):
        tournament = super().save(commit=commit)
        if commit:
            # Get editor users from cleaned data
            editor_users = self.cleaned_data.get("editor_usernames", [])
            # Always include owner as editor
            all_editors = set(editor_users)
            all_editors.add(tournament.owner)
            tournament.editors.set(all_editors)

            # Handle divisions
            division_names = self.cleaned_data.get("division_names", [])
            existing_divisions = {d.name: d for d in tournament.divisions.all()}

            # Add new divisions
            for name in division_names:
                if name not in existing_divisions:
                    Division.objects.create(tournament=tournament, name=name)

            # Remove divisions not in the list
            for name, division in existing_divisions.items():
                if name not in division_names:
                    division.delete()

        return tournament


class ResultSlipForm(forms.ModelForm):
    """Form for entering game results."""

    class Meta:
        model = ResultSlip
        fields = ["round", "winner", "winner_score", "loser", "loser_score", "winner_started"]

    def __init__(self, *args, division=None, **kwargs):
        super().__init__(*args, **kwargs)
        if division:
            entrants = Entrant.objects.filter(division=division)
            self.fields["winner"].queryset = entrants
            self.fields["loser"].queryset = entrants
            self.fields["winner"].label_from_instance = lambda e: e.player.name
            self.fields["loser"].label_from_instance = lambda e: e.player.name
        elif self.instance.pk:
            entrants = Entrant.objects.filter(division=self.instance.division)
            self.fields["winner"].queryset = entrants
            self.fields["loser"].queryset = entrants
            self.fields["winner"].label_from_instance = lambda e: e.player.name
            self.fields["loser"].label_from_instance = lambda e: e.player.name

    def clean(self):
        cleaned_data = super().clean()
        winner = cleaned_data.get("winner")
        loser = cleaned_data.get("loser")
        if winner and loser and winner == loser:
            raise forms.ValidationError("Winner and loser must be different players.")
        return cleaned_data
