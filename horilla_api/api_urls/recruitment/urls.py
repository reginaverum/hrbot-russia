from django.urls import path

from horilla_api.api_views.recruitment.views import CandidateAIScreenView

urlpatterns = [
    path(
        "candidate/<int:pk>/ai-screen/",
        CandidateAIScreenView.as_view(),
        name="api-candidate-ai-screen",
    ),
]
