from django.contrib import admin

from .models import Division, Entrant, Pairing, Player, ResultSlip, RoundPairings, Tournament


class DivisionInline(admin.TabularInline):
    model = Division
    extra = 1


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "start_date", "owner")
    list_filter = ("start_date",)
    search_fields = ("name", "location")
    filter_horizontal = ("editors",)
    inlines = [DivisionInline]


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("name", "player_number", "rating")
    search_fields = ("name", "player_number")


@admin.register(Entrant)
class EntrantAdmin(admin.ModelAdmin):
    list_display = ("number", "player", "division")
    list_filter = ("division",)
    search_fields = ("player__name",)


@admin.register(RoundPairings)
class RoundPairingsAdmin(admin.ModelAdmin):
    list_display = ("division", "round", "status")
    list_filter = ("division", "status")


@admin.register(Pairing)
class PairingAdmin(admin.ModelAdmin):
    list_display = ("round", "first", "second", "table", "table_label", "division")
    list_filter = ("division", "round")


@admin.register(ResultSlip)
class ResultSlipAdmin(admin.ModelAdmin):
    list_display = ("round", "winner", "winner_score", "loser", "loser_score", "division")
    list_filter = ("division", "round")
    search_fields = ("winner__player__name", "loser__player__name")
