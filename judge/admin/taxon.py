from django.contrib import admin


class ProblemGroupAdmin(admin.ModelAdmin):
    fields = ("name", "full_name")


class ProblemTypeAdmin(admin.ModelAdmin):
    fields = ("name", "full_name")


class OfficialContestCategoryAdmin(admin.ModelAdmin):
    fields = ("name",)


class OfficialContestLocationAdmin(admin.ModelAdmin):
    fields = ("name",)
