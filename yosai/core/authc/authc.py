"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
from collections import defaultdict
import logging
from passlib.context import CryptContext
from passlib.totp import OTPContext, TokenError, TOTP

from yosai.core import (
    EVENT_TOPIC,
    AccountException,
    AdditionalAuthenticationRequired,
    AuthenticationSettings,
    AuthenticationAttempt,
    first_realm_successful_strategy,
    IncorrectCredentialsException,
    InvalidAuthenticationSequenceException,
    LockedAccountException,
    authc_abcs,
    realm_abcs,
)

logger = logging.getLogger(__name__)


class UsernamePasswordToken(authc_abcs.AuthenticationToken):

    def __init__(self, username, password, remember_me=False, host=None):
        """
        :param username: the username submitted for authentication
        :type username: str

        :param password: the credentials submitted for authentication
        :type password: bytearray or string

        :param remember_me:  if the user wishes their identity to be
                             remembered across sessions
        :type remember_me: bool
        :param host:     the host name or IP string from where the attempt
                         is occuring
        :type host: str
        """
        self.identifier = username
        self.credentials = password
        self.host = host
        self.is_remember_me = remember_me

    @property
    def identifier(self):
        return self._identifier

    @identifier.setter
    def identifier(self, identifier):
        if not identifier:
            raise ValueError('Username must be defined')

        self._identifier = identifier

    @property
    def credentials(self):
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        if isinstance(credentials, bytes):
            self._credentials = credentials
        if isinstance(credentials, str):
            self._credentials = bytes(credentials, 'utf-8')
        else:
            raise ValueError('Password must be a str or bytes')

    def __repr__(self):
        result = "{0} - {1}, remember_me={2}".format(
            self.__class__.__name__, self.identifier, self.is_remember_me)
        if (self.host):
            result += ", ({0})".format(self.host)
        return result


class TOTPToken(authc_abcs.AuthenticationToken):

    def __init__(self, totp_token, remember_me=False):
        """
        :param totp_key: the 6-digit token generated by the client, keyed using
                         the client's private key
        :type totp_key: int
        """
        self.credentials = totp_token
        self.is_remember_me = remember_me

    @property
    def credentials(self):
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        try:
            assert 99999 < credentials < 1000000
            self._credentials = credentials
        except (TypeError, AssertionError) as exc:
            msg = 'TOTPToken must be a 6-digit int. Got: ', str(credentials)
            raise exc.__class__(msg)

# the verify field corresponds to the human intelligible name of the credential type,
# stored in the database (this design is TBD)
token_info = {UsernamePasswordToken: {'tier': 1, 'cred_type': 'password'},
              TOTPToken: {'tier': 2, 'cred_type': 'totp_key'}}


class DefaultAuthenticator(authc_abcs.Authenticator):

    # Unlike Shiro, Yosai injects the strategy and the eventbus
    def __init__(self,
                 settings,
                 strategy=first_realm_successful_strategy):

        self.authc_settings = AuthenticationSettings(settings)
        self.authentication_strategy = strategy

        if self.authc_settings.mfa_challenger:
            self.mfa_challenger = self.authc_settings.mfa_challenger()

        self.realms = None
        self.token_realm_resolver = None
        self.locking_realm = None
        self.locking_limit = None
        self.event_bus = None

    def init_realms(self, realms):
        """
        :type realms: Tuple
        """
        self.realms = tuple(realm for realm in realms
                            if isinstance(realm, realm_abcs.AuthenticatingRealm))
        self.register_cache_clear_listener()
        self.token_realm_resolver = self.init_token_resolution()
        self.init_locking()

    def init_locking(self):
        locking_limit = self.authc_settings.account_lock_threshold
        if locking_limit:
            self.locking_realm = self.locate_locking_realm()  # for account locking
            self.locking_limit = locking_limit

    def init_token_resolution(self):
        token_resolver = defaultdict(list)
        for realm in self.realms:
            if isinstance(realm, realm_abcs.AuthenticatingRealm):
                for token_class in realm.supported_authc_tokens:
                    token_resolver[token_class].append(realm)
        return token_resolver

    def locate_locking_realm(self):
        """
        the first realm that is identified as a LockingRealm will be used to
        lock all accounts
        """
        for realm in self.realms:
            if hasattr(realm, 'lock_account'):
                return realm
        return None

    def authenticate_single_realm_account(self, realm, authc_token):
        return realm.authenticate_account(authc_token)

    def authenticate_multi_realm_account(self, realms, authc_token):
        attempt = AuthenticationAttempt(authc_token, realms)
        return self.authentication_strategy(attempt)

    def authenticate_account(self, identifiers, authc_token):
        """
        :type identifiers: SimpleIdentifierCollection or None

        :returns: account_id (identifiers) if the account authenticates
        :rtype: SimpleIdentifierCollection
        """
        msg = ("Authentication submission received for authentication "
               "token [" + str(authc_token) + "]")
        logger.debug(msg)

        # the following conditions verify correct authentication sequence
        if not getattr(authc_token, 'identifier', None):
            if not identifiers:
                msg = "Authentication must be performed in expected sequence."
                raise InvalidAuthenticationSequenceException(msg)
            authc_token.identifier = identifiers.primary_identifier

        # add token metadata before sending it onward:
        authc_token.token_info = token_info[authc_token.__class__]

        try:
            account = self.do_authenticate_account(authc_token)
            if (account is None):
                msg2 = ("No account returned by any configured realms for "
                        "submitted authentication token [{0}]".
                        format(authc_token))

                raise AccountException(msg2)

        except AdditionalAuthenticationRequired as exc:
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.PROGRESS')
            try:
                self.mfa_challenger.send_challenge(authc_token.identifier)
            except AttributeError:
                # implies no multi-factor authc challenger is set
                pass
            raise exc  # the security_manager saves subject identifiers

        except AccountException:
            self.notify_event(authc_token.identifier,
                              'AUTHENTICATION.ACCOUNT_NOT_FOUND')
            raise

        except LockedAccountException:
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.FAILED')
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.ACCOUNT_LOCKED')
            raise

        except IncorrectCredentialsException as exc:
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.FAILED')
            self.validate_locked(authc_token, exc.failed_attempts)
            # this won't be called if the Account is locked:
            raise IncorrectCredentialsException

        self.notify_event(account['account_id'].primary_identifier,
                          'AUTHENTICATION.SUCCEEDED')

        return account['account_id']

    def do_authenticate_account(self, authc_token):
        """
        Returns an account object only when the current token authenticates AND
        the authentication process is complete, raising otherwise

        :returns:  Account
        :raises AdditionalAuthenticationRequired: when additional tokens are required,
                                                  passing the account object
        """
        try:
            realms = self.token_realm_resolver[authc_token.__class__]
        except KeyError:
            raise KeyError('Unsupported Token Type Provided: ', authc_token.__class__.__name__)

        if (len(self.realms) == 1):
            account = self.authenticate_single_realm_account(realms[0], authc_token)
        else:
            account = self.authenticate_multi_realm_account(self.realms, authc_token)

        cred_type = authc_token.token_info['cred_type']
        attempts = account['authc_info'][cred_type].get('failed_attempts', [])
        self.validate_locked(authc_token, attempts)

        # the following condition verifies whether the account uses MFA:
        if len(account['authc_info']) > authc_token.token_info['tier']:
            # the token authenticated but additional authentication is required
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.PROGRESS')
            raise AdditionalAuthenticationRequired(account['account_id'])

        return account
    # --------------------------------------------------------------------------
    # Event Communication
    # --------------------------------------------------------------------------

    def clear_cache(self, items=None, topic=EVENT_TOPIC):
        """
        expects event object to be in the format of a session-stop or
        session-expire event, whose results attribute is a
        namedtuple(identifiers, session_key)
        """
        try:
            for realm in self.realms:
                identifier = items.identifiers.from_source(realm.name)
                if identifier:
                    realm.clear_cached_authc_info(identifier)
        except AttributeError:
            msg = ('Could not clear authc_info from cache after event. '
                   'items: ' + str(items))
            logger.warn(msg)

    def register_cache_clear_listener(self):
        try:
            self.event_bus.subscribe(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.isSubscribed(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.subscribe(self.clear_cache, 'SESSION.STOP')
            self.event_bus.isSubscribed(self.clear_cache, 'SESSION.STOP')

        except AttributeError:
            msg = "Authenticator failed to register listeners to event bus"
            logger.debug(msg)

    def notify_event(self, identifier, topic):
        try:
            self.event_bus.sendMessage(topic, identifier=identifier)
        except AttributeError:
            msg = "Could not publish {} event".format(topic)
            raise AttributeError(msg)

    def validate_locked(self, authc_token, failed_attempts):
        """
        :param failed_attempts:  the failed attempts for this type of credential
        """
        if self.locking_limit and len(failed_attempts) > self.locking_limit:
            msg = ('Authentication attempts breached threshold.  Account'
                   ' is now locked for: ' + str(authc_token.identifier))
            self.locking_realm.lock_account(authc_token.identifier)
            self.notify_event(authc_token.identifier, 'AUTHENTICATION.ACCOUNT_LOCKED')
            raise LockedAccountException(msg)

    def __repr__(self):
        return "<DefaultAuthenticator(event_bus={0}, strategy={0})>".\
            format(self.event_bus, self.authentication_strategy)


class PasslibVerifier(authc_abcs.CredentialsVerifier):

    def __init__(self, settings):
        authc_settings = AuthenticationSettings(settings)
        self.password_cc = self.create_password_crypt_context(authc_settings)
        self.totp_cc = self.create_totp_crypt_context(authc_settings)
        self.cc_token_resolver = {UsernamePasswordToken: self.password_cc,
                                  TOTPToken: self.totp_cc}
        self.supported_tokens = self.cc_token_resolver.keys()

    def verify_credentials(self, authc_token, authc_info):
        submitted = authc_token.credentials
        stored = self.get_stored_credentials(authc_token, authc_info)
        service = self.cc_token_resolver[authc_token.__class__]

        try:
            if isinstance(authc_token, UsernamePasswordToken):
                result = service.verify(submitted, stored)
                if not result:
                    raise IncorrectCredentialsException
            else:
                totp = TOTP(key=stored)
                totp.verify(submitted)

        except (ValueError, TokenError):
            raise IncorrectCredentialsException

    def get_stored_credentials(self, authc_token, authc_info):
        # look up the db credential type assigned to this type token:
        cred_type = authc_token.token_info['cred_type']

        try:
            return authc_info[cred_type]['credential']

        except KeyError:
            msg = "{0} is required but unavailable from authc_info".format(cred_type)
            raise KeyError(msg)

    def create_password_crypt_context(self, authc_settings):
        context = dict(schemes=[authc_settings.preferred_algorithm])
        context.update(authc_settings.preferred_algorithm_context)
        return CryptContext(**context)

    def create_totp_crypt_context(self, saved_key):
        pass
        # context = authc_settings.totp_context
        # return OTPContext(**context).new(type='totp')
