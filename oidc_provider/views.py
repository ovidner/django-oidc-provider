import logging

from Crypto.PublicKey import RSA
from django.contrib.auth.views import redirect_to_login, logout
from django.core.urlresolvers import reverse
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods
from django.views.generic import View
from jwkest import long_to_base64

from oidc_provider.lib.claims import StandardScopeClaims
from oidc_provider.lib.endpoints.authorize import *
from oidc_provider.lib.endpoints.token import *
from oidc_provider.lib.errors import *
from oidc_provider.lib.utils.common import redirect, get_issuer
from oidc_provider.lib.utils.oauth2 import protected_resource_view
from oidc_provider.models import Client, RSAKey
from oidc_provider import settings


logger = logging.getLogger(__name__)


class AuthorizeView(View):

    def get(self, request, *args, **kwargs):

        authorize = AuthorizeEndpoint(request)

        try:
            authorize.validate_params()

            if request.user.is_authenticated():
                # Check if there's a hook setted.
                hook_resp = settings.get('OIDC_AFTER_USERLOGIN_HOOK', import_str=True)(
                    request=request, user=request.user,
                    client=authorize.client)
                if hook_resp:
                    return hook_resp

                if settings.get('OIDC_SKIP_CONSENT_ALWAYS'):
                    return redirect(authorize.create_response_uri())

                if settings.get('OIDC_SKIP_CONSENT_ENABLE'):
                    # Check if user previously give consent.
                    if authorize.client_has_user_consent():
                        return redirect(authorize.create_response_uri())

                # Generate hidden inputs for the form.
                context = {
                    'params': authorize.params,
                }
                hidden_inputs = render_to_string(
                    'oidc_provider/hidden_inputs.html', context)

                # Remove `openid` from scope list
                # since we don't need to print it.
                authorize.params.scope.remove('openid')

                context = {
                    'client': authorize.client,
                    'hidden_inputs': hidden_inputs,
                    'params': authorize.params,
                }

                return render(request, 'oidc_provider/authorize.html', context)
            else:
                path = request.get_full_path()
                return redirect_to_login(path)

        except (ClientIdError, RedirectUriError) as error:
            context = {
                'error': error.error,
                'description': error.description,
            }

            return render(request, 'oidc_provider/error.html', context)

        except (AuthorizeError) as error:
            uri = error.create_uri(
                authorize.params.redirect_uri,
                authorize.params.state)

            return redirect(uri)

    def post(self, request, *args, **kwargs):

        authorize = AuthorizeEndpoint(request)

        allow = True if request.POST.get('allow') else False

        try:
            authorize.validate_params()
            
            if not allow:
                raise AuthorizeError(authorize.params.redirect_uri,
                                     'access_denied',
                                     authorize.grant_type)

            # Save the user consent given to the client.
            authorize.set_client_user_consent()

            uri = authorize.create_response_uri()

            return redirect(uri)

        except (AuthorizeError) as error:
            uri = error.create_uri(
                authorize.params.redirect_uri,
                authorize.params.state)

            return redirect(uri)


class TokenView(View):

    def post(self, request, *args, **kwargs):
        
        token = TokenEndpoint(request)

        try:
            token.validate_params()

            dic = token.create_response_dic()

            return TokenEndpoint.response(dic)

        except (TokenError) as error:
            return TokenEndpoint.response(error.create_dict(), status=400)


@require_http_methods(['GET', 'POST'])
@protected_resource_view(['openid'])
def userinfo(request, *args, **kwargs):
    """
    Create a diccionary with all the requested claims about the End-User.
    See: http://openid.net/specs/openid-connect-core-1_0.html#UserInfoResponse

    Return a diccionary.
    """
    token = kwargs['token']

    dic = {
        'sub': token.id_token.get('sub'),
    }

    standard_claims = StandardScopeClaims(token.user, token.scope)

    dic.update(standard_claims.create_response_dic())

    extra_claims = settings.get('OIDC_EXTRA_SCOPE_CLAIMS', import_str=True)(
        token.user, token.scope)

    dic.update(extra_claims.create_response_dic())

    response = JsonResponse(dic, status=200)
    response['Cache-Control'] = 'no-store'
    response['Pragma'] = 'no-cache'
    return response


class ProviderInfoView(View):

    def get(self, request, *args, **kwargs):
        dic = dict()

        dic['issuer'] = get_issuer()

        SITE_URL = settings.get('SITE_URL')

        dic['authorization_endpoint'] = SITE_URL + reverse('oidc_provider:authorize')
        dic['token_endpoint'] = SITE_URL + reverse('oidc_provider:token')
        dic['userinfo_endpoint'] = SITE_URL + reverse('oidc_provider:userinfo')
        dic['end_session_endpoint'] = SITE_URL + reverse('oidc_provider:logout')

        types_supported = [x[0] for x in Client.RESPONSE_TYPE_CHOICES]
        dic['response_types_supported'] = types_supported

        dic['jwks_uri'] = SITE_URL + reverse('oidc_provider:jwks')

        dic['id_token_signing_alg_values_supported'] = ['RS256']

        # See: http://openid.net/specs/openid-connect-core-1_0.html#SubjectIDTypes
        dic['subject_types_supported'] = ['public']

        dic['token_endpoint_auth_methods_supported'] = [ 'client_secret_post',
                                                         'client_secret_basic' ]

        return JsonResponse(dic)


class JwksView(View):

    def get(self, request, *args, **kwargs):
        dic = dict(keys=[])

        for rsakey in RSAKey.objects.all():
            public_key  = RSA.importKey(rsakey.key).publickey()
            dic['keys'].append({
                'kty': 'RSA',
                'alg': 'RS256',
                'use': 'sig',
                'kid': rsakey.kid,
                'n': long_to_base64(public_key.n),
                'e': long_to_base64(public_key.e),
            })

        response = JsonResponse(dic)
        response['Access-Control-Allow-Origin'] = '*'

        return response


class LogoutView(View):

    def get(self, request, *args, **kwargs):
        # We should actually verify if the requested redirect URI is safe
        return logout(request, next_page=request.GET.get('post_logout_redirect_uri'))
