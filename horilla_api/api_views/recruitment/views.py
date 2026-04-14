"""
horilla_api/api_views/recruitment/views.py

API views для модуля recruitment с AI-возможностями.
"""

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from recruitment.models import AIScreeningResult, Candidate


class CandidateAIScreenView(APIView):
    """
    POST /api/recruitment/candidate/{pk}/ai-screen/

    Запускает AI-скрининг резюме кандидата через YandexGPT.
    Результат сохраняется в AIScreeningResult (обновляется при повторном вызове).

    Требует аутентификации. Необходимо наличие резюме у кандидата.

    Responses:
        200: Скрининг выполнен успешно, возвращает результат.
        400: Резюме отсутствует или YandexGPT вернул невалидный ответ.
        404: Кандидат не найден.
        500: Ошибка конфигурации или сетевая ошибка.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            candidate = Candidate.objects.get(pk=pk)
        except Candidate.DoesNotExist:
            return Response(
                {"error": "Кандидат не найден."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Импортируем здесь, чтобы ошибки конфигурации проявились только при вызове
        try:
            from hrbot.ai_screening import screen_resume
        except ImportError as exc:
            return Response(
                {"error": f"Модуль AI-скрининга недоступен: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            result = screen_resume(candidate)
        except EnvironmentError as exc:
            return Response(
                {"error": f"Ошибка конфигурации YandexGPT: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Сохраняем / обновляем результат в БД
        screening, _ = AIScreeningResult.objects.update_or_create(
            candidate_id=candidate,
            defaults={
                "score": result["score"],
                "matched_skills": result["matched_skills"],
                "missing_skills": result["missing_skills"],
                "summary": result["summary"],
            },
        )

        return Response(
            {
                "candidate_id": candidate.pk,
                "candidate_name": candidate.name,
                "score": screening.score,
                "matched_skills": screening.matched_skills,
                "missing_skills": screening.missing_skills,
                "summary": screening.summary,
                "screened_at": screening.screened_at,
                "resume_text_length": result["resume_text_length"],
            },
            status=status.HTTP_200_OK,
        )
