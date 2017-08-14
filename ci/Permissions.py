
# Copyright 2016 Battelle Energy Alliance, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import TimeUtils
import logging
from ci import models
from django.conf import settings
logger = logging.getLogger('ci')

def is_collaborator(auth, request_session, build_user, repo, auth_session=None, user=None):
    """
    Checks to see if the signed in user is a collaborator on a repo.
    This will cache the value for a time specified by settings.COLLABORATOR_CACHE_TIMEOUT
    Input:
      auth: an oauth_api.OAuth derived class from one of the git servers
      request_session: A session from HttpRequest.session
      build_user: models.GitUser who has access to check collaborators
      repo: models.Repository to check against
      auth_session: OAuth2Session: optional if there is already a session started
      user: models.GitUser: User to check for. If None then the user will be pulled from the request_session
    Return:
      (bool, models.GitUser) tuple: bool is whether the user is a collaborator
        GitUser is the user from the request_session or None if not signed in
    """
    try:
        if not user:
            user = auth.signed_in_user(repo.user.server, request_session)
        if not user:
            return False, None

        if auth._collaborators_key in request_session:
            collab_dict = request_session[auth._collaborators_key]
            val = collab_dict.get(str(repo))
            timestamp = TimeUtils.get_local_timestamp()
            # Check to see if their permissions are still valid
            if val and timestamp < val[1]:
                return val[0], user

        api = repo.user.server.api()
        if auth_session == None:
            auth_session = auth.start_session_for_user(build_user)

        collab_dict = request_session.get(auth._collaborators_key, {})
        val = api.is_collaborator(auth_session, user, repo)
        collab_dict[str(repo)] = (val, TimeUtils.get_local_timestamp() + settings.COLLABORATOR_CACHE_TIMEOUT)
        request_session[auth._collaborators_key] = collab_dict
        logger.info("Is collaborator for user %s on %s: %s" % (user, repo, val))
        return val, user
    except Exception as e:
        logger.warning("Failed to check collbaborater for %s: %s" % (user, e))
        return False, None

def job_permissions(session, job, auth_session=None, user=None):
    """
    Logic for a job to see who can see results, activate,
    cancel, invalidate, or owns the job.
    """
    ret_dict = {'is_owner': False,
        'can_see_results': False,
        'can_admin': False,
        'can_activate': False,
        'can_see_client': False,
          }
    auth = job.event.base.server().auth()
    repo = job.recipe.repository
    if not user:
        user = auth.signed_in_user(repo.user.server, session)

    ret_dict['can_see_client'] = is_allowed_to_see_clients(session)

    if user == job.recipe.build_user:
        ret_dict['is_owner'] = user == job.recipe.build_user
        ret_dict['can_admin'] = True
        ret_dict['can_see_results'] = True
        ret_dict['can_activate'] = True
        return ret_dict

    ret_dict['can_see_results'] = can_see_results(session, job.recipe)

    if not user:
        return ret_dict

    if job.recipe.automatic == models.Recipe.AUTO_FOR_AUTHORIZED:
        if user in job.recipe.auto_authorized.all():
            ret_dict['can_activate'] = True

    if job.recipe.private and ret_dict['can_see_results']:
        ret_dict['can_admin'] = True
        ret_dict['can_activate'] = True
    elif not job.recipe.private:
        collab, user = is_collaborator(auth, session, job.event.build_user, repo, auth_session=auth_session, user=user)
        if collab:
            ret_dict['can_admin'] = True
            ret_dict['can_activate'] = True
    return ret_dict

def can_see_results(session, recipe):
    """
    Checks to see if the signed in user can see the results of a recipe
    Input:
      request: HttpRequest
      recipe: models.Recipe to check against
    Return:
      On error, an HttpResponse. Else None.
    """
    if not recipe.private:
        return True

    build_user = recipe.build_user
    signed_in = build_user.server.auth().signed_in_user(build_user.server, session)

    if signed_in == build_user:
        return True

    if not signed_in:
        return False

    auth = build_user.server.auth()
    api = signed_in.server.api()
    auth_session = auth.start_session_for_user(signed_in)

    # if viewable_by_teams was specified we check if
    # the signed in user is a member of one of the teams
    for team in recipe.viewable_by_teams.all():
        if team == signed_in.name or is_team_member(session, api, auth_session, team, signed_in):
            return True

    if recipe.viewable_by_teams.count():
        return False

    # No viewable_by_teams was specified. They need to be
    # a collaborator on the repository
    collab, user = is_collaborator(auth, session, build_user, recipe.repository, user=signed_in)
    if not collab:
        return False

    return True

def is_allowed_to_cancel(session, ev):
    """
    A convience function to check to see if the signed in user is allowed to cancel an event.
    Input:
      session: session from HttpRequest
      ev: models.Event to check against
    Return:
      (bool, models.GitUser) tuple: bool is whether they are allowed. Gituser is the signed in user or None if not signed in.
    """
    auth = ev.base.server().auth()
    allowed, signed_in_user = is_collaborator(auth, session, ev.build_user, ev.base.branch.repository)
    return allowed, signed_in_user


def is_team_member(session, api, auth, team, user):
    """
    Checks to see if a user is a team member and caches the results
    """
    teams = session.get("teams", {})
    # Check to see if their permissions are still valid
    if teams and team in teams and TimeUtils.get_local_timestamp() < teams[team][1]:
        return teams[team][0]

    is_member = api.is_member(auth, team, user)
    logger.info("User '%s' member status of '%s': %s" % (user, team, is_member))
    teams[team] = (is_member, TimeUtils.get_local_timestamp() + settings.COLLABORATOR_CACHE_TIMEOUT)
    session["teams"] = teams
    return is_member

def is_allowed_to_see_clients(session):
    """
    Check to see if the signed in user can see client information.
    We do this by checking the settings.AUTHORIZED_USERS
    AUTHORIZED_USERS can contain orgs and teams
    """
    val = session.get("allowed_to_see_clients")
    # Check to see if their permissions are still valid
    if val and TimeUtils.get_local_timestamp() < val[1]:
        return val[0]

    user = None

    for server in settings.INSTALLED_GITSERVERS:
        gitserver = models.GitServer.objects.get(host_type=server)
        auth = gitserver.auth()
        user = auth.signed_in_user(gitserver, session)
        if not user:
            continue

        auth_session = gitserver.auth().start_session_for_user(user)
        for authed_user in settings.AUTHORIZED_USERS:
            if user.name == authed_user or is_team_member(session, gitserver.api(), auth_session, authed_user, user):
                logger.info("'%s' is a member of '%s' and is allowed to see clients" % (user, authed_user))
                session["allowed_to_see_clients"] = (True, TimeUtils.get_local_timestamp() + settings.COLLABORATOR_CACHE_TIMEOUT)
                return True
        logger.info("%s is NOT allowed to see clients on %s" % (user, gitserver))
    session["allowed_to_see_clients"] = (False, TimeUtils.get_local_timestamp() + settings.COLLABORATOR_CACHE_TIMEOUT)
    if user:
        logger.info("%s is NOT allowed to see clients" % user)
    return False
