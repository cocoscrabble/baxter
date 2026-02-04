from django import forms

from users.models import User

from .models import Tournament


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
        usernames_text = self.cleaned_data.get("editor_usernames", "")
        if not usernames_text.strip():
            return []

        usernames = [u.strip() for u in usernames_text.split("\n") if u.strip()]
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
            # Get editor users from cleaned data
            editor_users = self.cleaned_data.get("editor_usernames", [])
            # Always include owner as editor
            all_editors = set(editor_users)
            all_editors.add(tournament.owner)
            tournament.editors.set(all_editors)
        return tournament
