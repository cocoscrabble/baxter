from django.contrib import admin

from .models import Tournament


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "start_date", "owner")
    list_filter = ("start_date",)
    search_fields = ("name", "location")
    filter_horizontal = ("editors",)
