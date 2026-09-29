import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from stations.route_planner import RoutePlanningError, plan_route


@csrf_exempt
@require_POST
def route_plan(request):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Request body must be valid JSON."}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "Request body must be a JSON object."}, status=400)

    start = payload.get("start")
    finish = payload.get("finish")
    if not isinstance(start, str) or not start.strip():
        return JsonResponse({"error": "'start' must be a non-empty US location."}, status=400)
    if not isinstance(finish, str) or not finish.strip():
        return JsonResponse({"error": "'finish' must be a non-empty US location."}, status=400)

    try:
        result = plan_route(start.strip(), finish.strip())
    except RoutePlanningError as exc:
        return JsonResponse({"error": str(exc), "code": exc.code}, status=exc.status_code)

    return JsonResponse(result)