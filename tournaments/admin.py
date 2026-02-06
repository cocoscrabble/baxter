from django.contrib import admin

from .models import Division, Player, Tournament


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
