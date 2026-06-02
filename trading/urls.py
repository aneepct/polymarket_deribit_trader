from django.urls import path
from .views import PositionsView, StateView

urlpatterns = [
    path("positions/", PositionsView.as_view(), name="positions"),
    path("trader/state/", StateView.as_view(), name="trader-state"),
]
