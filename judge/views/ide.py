from django.utils.translation import gettext as _
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt

import requests, json, http

PREFIX_URL = 'ide/api'

@login_required
@csrf_exempt
def api(request):
    url = 'http://localhost:2358' + request.get_full_path()[len(PREFIX_URL) + 1:]
    headers = {'X-Judge0-Token': 'cuom1999'}
    r = None
    if request.method == 'POST':
        r = requests.post(url, data=json.loads(request.body.decode('utf-8')), headers=headers)
    elif request.method == 'GET':
        r = requests.get(url, headers=headers)
    else:
        return Http404()

    res = r.content.decode('utf-8')
    try:
        res = json.loads(r.content.decode('utf-8'))
        return JsonResponse(res, status=r.status_code, safe=False)
    except Exception:
        return HttpResponse(res)
    