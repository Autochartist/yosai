import copy
import traceback

from yosai import (
    MultiRealmAuthenticationException,
    AuthenticationException,
)

from . import (
    DefaultCompositeAccount,
    IAuthenticationAttempt,
    IAuthenticationStrategy,
)

class DefaultAuthenticationAttempt(IAuthenticationAttempt, object):

    def __init__(self, authc_token, realms):
        """
        :param authc_token: cannot be None
        :type authc_token:  AuthenticationToken
        :param realms:  cannot be None or Empty
        :type realms: Set 
        """
        self.authentication_token = authc_token
        self.realms = realms  # DG:  frozenset is another option

    # DG:  these accessor methods, and the attempt interface in general, seem
    # unecessary so I may remove later.  Presently, they're placeholders.
    @property
    def authentication_token(self):
        return self.authentication_token 

    @property
    def realms(self):
        return self.realms


class AllRealmsSuccessfulStrategy(IAuthenticationStrategy, object):
    
    def execute(self, authc_attempt):
        token = authc_attempt.authentication_token
        first_account_realm_name = None
        first_account = None
        composite_account = None

        for realm in authc_attempt.realms:
            if (realm.supports(token)):

                """
                If the realm throws an exception, the loop will short circuit
                and this method will return.  As an 'all successful' strategy,
                if there is even a single exception thrown by any of the
                supported realms, the authentication attempt is unsuccessful.
                This particular implementation also favors short circuiting
                immediately (instead of trying all realms and then aggregating
                all potential exceptions) because continuing to access
                additional account stores is likely to incur unnecessary /
                undesirable I/O for most apps.
                """
                account = realm.authenticate_account(token)

                if (account):
                    if (not first_account):
                        first_account = account
                        first_account_realm_name = realm.name
                    else:                    
                        if (not composite_account):
                            composite_account = DefaultCompositeAccount()
                            composite_account.append_realm_account(
                                first_account_realm_name, first_account)
                            
                        composite_account.append_realm_account(
                            realm.name, account) 
                
        if (composite_account):
            return composite_account

        return first_account


class AtLeastOneRealmSuccessfulStrategy(IAuthenticationStrategy, object):

    def execute(self, authc_attempt):
        """
        :rtype:  Account
        """
        authc_token = copy.copy(authc_attempt.authentication_token)
        realm_errors = {} 
        account = None
        first_account = None
        composite_account = None

        for realm in authc_attempt.realms:
            if (realm.supports(authc_token)):
                realm_name = realm.name
                try:
                    account = realm.authenticate_account(authc_token)
                except Exception as ex:
                    # noinspection ThrowableResultOfMethodCallIgnored
                    realm_errors[realm_name] = ex
                
                if (account is not None):
                    if (first_account is None): 
                        first_account = account
                    else:
                        if (composite_account is None):
                            composite_account = DefaultCompositeAccount()
                        composite_account.append_realm_account(
                            realm_name, account)

        if (composite_account is not None):
            return composite_account

        if (first_account is not None): 
            return first_account

        if (self.realm_errors):
            raise MultiRealmAuthenticationException(realm_errors)

        return None 


class FirstRealmSuccessfulStrategy(IAuthenticationStrategy, object):

    """
     The FirstRealmSuccessfulStrategy will iterate over the available realms
     and invoke Realm.authenticate_account(authc_token) on each one. The moment 
     that a realm returns an Account without raising an Exception, that account
     is returned immediately and all subsequent realms ignored entirely
     (iteration 'short circuits').

     If no realms return an Account:
         * If only one exception was thrown by any consulted Realm, that
           exception is thrown.
         * If more than one Realm threw an exception during consultation, those
           exceptions are bundled together as a
           MultiRealmAuthenticationException and that exception is thrown.
         * If no exceptions were thrown, None is returned, indicating to the
           calling Authenticator that no Account was found.
    """
    def __init__(self):
        pass

    def execute(self, authc_attempt):
        """
        :type authc_attempt:  AuthenticationAttempt
        :returns:  Account
        """
        authc_token = authc_attempt.authentication_token
        realm_errors = {} 
        account = None

        for realm in authc_attempt.realms:
            if (realm.supports(authc_token)):
                try:
                    account = realm.authenticate_account(authc_token)
                except Exception as ex:
                    realm_errors[realm.name] = ex
                    # current realm failed - try the next one:
                else:
                    if (account):
                        # successfully acquired an account
                        # -- stop iterating, return immediately:
                        return account

        if (realm_errors):
            if (len(realm_errors) == 1):
                exc = next(iter(realm_errors.values()))
                if (isinstance(exc, AuthenticationException)):
                    raise exc  # DG:  not sure.. TBD
                
                raise AuthenticationException(
                    "Unable to authenticate realm account.", exc)

            #  else more than one throwable encountered:
            else:
                raise MultiRealmAuthenticationException(realm_errors)

        return None 
