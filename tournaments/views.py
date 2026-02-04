from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render

from .forms import TournamentForm
from .models import Tournament


def tournament_list(request):
    """List all tournaments."""
    tournaments = Tournament.objects.all()
    return render(request, "tournaments/tournament_list.html", {"tournaments": tournaments})


def tournament_detail(request, pk):
    """View a single tournament."""
    tournament = get_object_or_404(Tournament, pk=pk)
    can_edit = request.user.is_authenticated and tournament.can_edit(request.user)
    return render(
        request,
        "tournaments/tournament_detail.html",
        {"tournament": tournament, "can_edit": can_edit},
    )


@login_required
def tournament_create(request):
    """Create a new tournament."""
    if request.method == "POST":
        form = TournamentForm(request.POST)
        if form.is_valid():
            tournament = form.save(commit=False)
            tournament.owner = request.user
            tournament.save()
            # Now save editors (including owner)
            form.save()
            return redirect("tournament_detail", pk=tournament.pk)
    else:
        form = TournamentForm()
    return render(request, "tournaments/tournament_form.html", {"form": form})


@login_required
def tournament_edit(request, pk):
    """Edit an existing tournament."""
    tournament = get_object_or_404(Tournament, pk=pk)
    if not tournament.can_edit(request.user):
        raise PermissionDenied

    if request.method == "POST":
        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            form.save()
            return redirect("tournament_detail", pk=tournament.pk)
    else:
        form = TournamentForm(instance=tournament)
    return render(
        request,
        "tournaments/tournament_form.html",
        {"form": form, "tournament": tournament},
    )


@login_required
def tournament_delete(request, pk):
    """Delete a tournament."""
    tournament = get_object_or_404(Tournament, pk=pk)
    if request.user != tournament.owner:
        raise PermissionDenied

    if request.method == "POST":
        tournament.delete()
        return redirect("tournament_list")
    return render(
        request,
        "tournaments/tournament_confirm_delete.html",
        {"tournament": tournament},
    )
