# -*- coding:utf-8 -*-
import base64
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Dict, Optional

from constants.languages import language_timezone_mapping, languages
from events.tenant_event import tenant_was_created
from extensions.ext_redis import redis_client
from flask import current_app
from libs.helper import get_remote_ip
from libs.passport import PassportService
from libs.password import compare_password, hash_password
from libs.rsa import generate_key_pair
from models.account import *
from services.errors.account import (AccountAlreadyInTenantError, AccountLoginError, AccountNotLinkTenantError,
                                     AccountRegisterError, CannotOperateSelfError, CurrentPasswordIncorrectError,
                                     InvalidActionError, LinkAccountIntegrateError, MemberNotInTenantError,
                                     NoPermissionError, RoleAlreadyAssignedError, TenantNotFound)
from sqlalchemy import func
from tasks.mail_invite_member_task import send_invite_member_mail_task
from werkzeug.exceptions import Forbidden
from sqlalchemy import exc


def _create_tenant_for_account(account) -> Tenant:
    tenant = TenantService.create_tenant(f"{account.name}'s Workspace")

    TenantService.create_tenant_member(tenant, account, role='owner')
    account.current_tenant = tenant

    return tenant


class AccountService:

    @staticmethod
    def load_user(user_id: str) -> Account:
        account = Account.query.filter_by(id=user_id).first()
        if not account:
            return None

        if account.status in [AccountStatus.BANNED.value, AccountStatus.CLOSED.value]:
            raise Forbidden('Account is banned or closed.')
        
        # init owner's tenant
        tenant_owner = TenantAccountJoin.query.filter_by(account_id=account.id, role='owner').first()
        if not tenant_owner:
            _create_tenant_for_account(account)
        
        current_tenant = TenantAccountJoin.query.filter_by(account_id=account.id, current=True).first()
        if current_tenant:
            account.current_tenant_id = current_tenant.tenant_id
        else:
            account.current_tenant_id = tenant_owner.tenant_id
            tenant_owner.current = True
            db.session.commit()
       
        if datetime.utcnow() - account.last_active_at > timedelta(minutes=10):
            account.last_active_at = datetime.utcnow()
            db.session.commit()

        return account


    @staticmethod
    def get_account_jwt_token(account):
        payload = {
            "user_id": account.id,
            "exp": datetime.utcnow() + timedelta(days=30),
            "iss": current_app.config['EDITION'],
            "sub": 'Console API Passport',
        }

        token = PassportService().issue(payload)
        return token

    @staticmethod
    def authenticate(email: str, password: str) -> Account:
        """authenticate account with email and password"""

        account = Account.query.filter_by(email=email).first()
        if not account:
            raise AccountLoginError('Invalid email or password.')

        if account.status == AccountStatus.BANNED.value or account.status == AccountStatus.CLOSED.value:
            raise AccountLoginError('Account is banned or closed.')

        if account.status == AccountStatus.PENDING.value:
            account.status = AccountStatus.ACTIVE.value
            account.initialized_at = datetime.utcnow()
            db.session.commit()

        if account.password is None or not compare_password(password, account.password, account.password_salt):
            raise AccountLoginError('Invalid email or password.')
        return account

    @staticmethod
    def update_account_password(account, password, new_password):
        """update account password"""
        if account.password and not compare_password(password, account.password, account.password_salt):
            raise CurrentPasswordIncorrectError("Current password is incorrect.")

        # generate password salt
        salt = secrets.token_bytes(16)
        base64_salt = base64.b64encode(salt).decode()

        # encrypt password with salt
        password_hashed = hash_password(new_password, salt)
        base64_password_hashed = base64.b64encode(password_hashed).decode()
        account.password = base64_password_hashed
        account.password_salt = base64_salt
        db.session.commit()
        return account

    @staticmethod
    def create_account(email: str, name: str, interface_language: str,
                       password: str = None,
                       interface_theme: str = 'light',
                       timezone: str = 'America/New_York', ) -> Account:
        """create account"""
        account = Account()
        account.email = email
        account.name = name

        if password:
            # generate password salt
            salt = secrets.token_bytes(16)
            base64_salt = base64.b64encode(salt).decode()

            # encrypt password with salt
            password_hashed = hash_password(password, salt)
            base64_password_hashed = base64.b64encode(password_hashed).decode()

            account.password = base64_password_hashed
            account.password_salt = base64_salt

        account.interface_language = interface_language
        account.interface_theme = interface_theme
        
        # Set timezone based on language
        account.timezone = language_timezone_mapping.get(interface_language, 'UTC') 

        db.session.add(account)
        db.session.commit()
        return account

    @staticmethod
    def link_account_integrate(provider: str, open_id: str, account: Account) -> None:
        """Link account integrate"""
        try:
            # Query whether there is an existing binding record for the same provider
            account_integrate: Optional[AccountIntegrate] = AccountIntegrate.query.filter_by(account_id=account.id,
                                                                                             provider=provider).first()

            if account_integrate:
                # If it exists, update the record
                account_integrate.open_id = open_id
                account_integrate.encrypted_token = ""  # todo
                account_integrate.updated_at = datetime.utcnow()
            else:
                # If it does not exist, create a new record
                account_integrate = AccountIntegrate(account_id=account.id, provider=provider, open_id=open_id,
                                                     encrypted_token="")
                db.session.add(account_integrate)

            db.session.commit()
            logging.info(f'Account {account.id} linked {provider} account {open_id}.')
        except Exception as e:
            logging.exception(f'Failed to link {provider} account {open_id} to Account {account.id}')
            raise LinkAccountIntegrateError('Failed to link account.') from e

    @staticmethod
    def close_account(account: Account) -> None:
        """todo: Close account"""
        account.status = AccountStatus.CLOSED.value
        db.session.commit()

    @staticmethod
    def update_account(account, **kwargs):
        """Update account fields"""
        for field, value in kwargs.items():
            if hasattr(account, field):
                setattr(account, field, value)
            else:
                raise AttributeError(f"Invalid field: {field}")

        db.session.commit()
        return account

    @staticmethod
    def update_last_login(account: Account, request) -> None:
        """Update last login time and ip"""
        account.last_login_at = datetime.utcnow()
        account.last_login_ip = get_remote_ip(request)
        db.session.add(account)
        db.session.commit()
        logging.info(f'Account {account.id} logged in successfully.')


class TenantService:

    @staticmethod
    def create_tenant(name: str) -> Tenant:
        """Create tenant"""
        tenant = Tenant(name=name)

        db.session.add(tenant)
        db.session.commit()

        tenant.encrypt_public_key = generate_key_pair(tenant.id)
        db.session.commit()
        return tenant

    @staticmethod
    def create_tenant_member(tenant: Tenant, account: Account, role: str = 'normal') -> TenantAccountJoin:
        """Create tenant member"""
        if role == TenantAccountJoinRole.OWNER.value:
            if TenantService.has_roles(tenant, [TenantAccountJoinRole.OWNER]):
                logging.error(f'Tenant {tenant.id} has already an owner.')
                raise Exception('Tenant already has an owner.')

        ta = TenantAccountJoin(
            tenant_id=tenant.id,
            account_id=account.id,
            role=role
        )
        db.session.add(ta)
        db.session.commit()
        return ta

    @staticmethod
    def get_join_tenants(account: Account) -> List[Tenant]:
        """Get account join tenants"""
        return db.session.query(Tenant).join(
            TenantAccountJoin, Tenant.id == TenantAccountJoin.tenant_id
        ).filter(TenantAccountJoin.account_id == account.id).all()

    @staticmethod
    def get_current_tenant_by_account(account: Account):
        """Get tenant by account and add the role"""
        tenant = account.current_tenant
        if not tenant:
            raise TenantNotFound("Tenant not found.")

        ta = TenantAccountJoin.query.filter_by(tenant_id=tenant.id, account_id=account.id).first()
        if ta:
            tenant.role = ta.role
        else:
            raise TenantNotFound("Tenant not found for the account.")
        return tenant

    @staticmethod
    def switch_tenant(account: Account, tenant_id: int = None) -> None:
        """Switch the current workspace for the account"""

        # Ensure tenant_id is provided
        if tenant_id is None:
            raise ValueError("Tenant ID must be provided.")

        tenant_account_join = TenantAccountJoin.query.filter_by(account_id=account.id, tenant_id=tenant_id).first()
        if not tenant_account_join:
            raise AccountNotLinkTenantError("Tenant not found or account is not a member of the tenant.")
        else: 
            TenantAccountJoin.query.filter(TenantAccountJoin.account_id == account.id, TenantAccountJoin.tenant_id != tenant_id).update({'current': False})
            tenant_account_join.current = True
            db.session.commit()
            # Set the current tenant for the account
            account.current_tenant_id = tenant_account_join.tenant_id

    @staticmethod
    def get_tenant_members(tenant: Tenant) -> List[Account]:
        """Get tenant members"""
        query = (
            db.session.query(Account, TenantAccountJoin.role)
            .select_from(Account)
            .join(
                TenantAccountJoin, Account.id == TenantAccountJoin.account_id
            )
            .filter(TenantAccountJoin.tenant_id == tenant.id)
        )

        # Initialize an empty list to store the updated accounts
        updated_accounts = []

        for account, role in query:
            account.role = role
            updated_accounts.append(account)

        return updated_accounts

    @staticmethod
    def has_roles(tenant: Tenant, roles: List[TenantAccountJoinRole]) -> bool:
        """Check if user has any of the given roles for a tenant"""
        if not all(isinstance(role, TenantAccountJoinRole) for role in roles):
            raise ValueError('all roles must be TenantAccountJoinRole')

        return db.session.query(TenantAccountJoin).filter(
            TenantAccountJoin.tenant_id == tenant.id,
            TenantAccountJoin.role.in_([role.value for role in roles])
        ).first() is not None

    @staticmethod
    def get_user_role(account: Account, tenant: Tenant) -> Optional[TenantAccountJoinRole]:
        """Get the role of the current account for a given tenant"""
        join = db.session.query(TenantAccountJoin).filter(
            TenantAccountJoin.tenant_id == tenant.id,
            TenantAccountJoin.account_id == account.id
        ).first()
        return join.role if join else None

    @staticmethod
    def get_tenant_count() -> int:
        """Get tenant count"""
        return db.session.query(func.count(Tenant.id)).scalar()

    @staticmethod
    def check_member_permission(tenant: Tenant, operator: Account, member: Account, action: str) -> None:
        """Check member permission"""
        perms = {
            'add': ['owner', 'admin'],
            'remove': ['owner'],
            'update': ['owner']
        }
        if action not in ['add', 'remove', 'update']:
            raise InvalidActionError("Invalid action.")

        if member:
            if operator.id == member.id:
                raise CannotOperateSelfError("Cannot operate self.")

        ta_operator = TenantAccountJoin.query.filter_by(
            tenant_id=tenant.id,
            account_id=operator.id
        ).first()

        if not ta_operator or ta_operator.role not in perms[action]:
            raise NoPermissionError(f'No permission to {action} member.')

    @staticmethod
    def remove_member_from_tenant(tenant: Tenant, account: Account, operator: Account) -> None:
        """Remove member from tenant"""
        if operator.id == account.id and TenantService.check_member_permission(tenant, operator, account, 'remove'):
            raise CannotOperateSelfError("Cannot operate self.")

        ta = TenantAccountJoin.query.filter_by(tenant_id=tenant.id, account_id=account.id).first()
        if not ta:
            raise MemberNotInTenantError("Member not in tenant.")

        db.session.delete(ta)

        account.initialized_at = None
        account.status = AccountStatus.PENDING.value
        account.password = None
        account.password_salt = None

        db.session.commit()

    @staticmethod
    def update_member_role(tenant: Tenant, member: Account, new_role: str, operator: Account) -> None:
        """Update member role"""
        TenantService.check_member_permission(tenant, operator, member, 'update')

        target_member_join = TenantAccountJoin.query.filter_by(
            tenant_id=tenant.id,
            account_id=member.id
        ).first()

        if target_member_join.role == new_role:
            raise RoleAlreadyAssignedError("The provided role is already assigned to the member.")

        if new_role == 'owner':
            # Find the current owner and change their role to 'admin'
            current_owner_join = TenantAccountJoin.query.filter_by(
                tenant_id=tenant.id,
                role='owner'
            ).first()
            current_owner_join.role = 'admin'

        # Update the role of the target member
        target_member_join.role = new_role
        db.session.commit()

    @staticmethod
    def dissolve_tenant(tenant: Tenant, operator: Account) -> None:
        """Dissolve tenant"""
        if not TenantService.check_member_permission(tenant, operator, operator, 'remove'):
            raise NoPermissionError('No permission to dissolve tenant.')
        db.session.query(TenantAccountJoin).filter_by(tenant_id=tenant.id).delete()
        db.session.delete(tenant)
        db.session.commit()

    @staticmethod
    def get_custom_config(tenant_id: str) -> None:
        tenant = db.session.query(Tenant).filter(Tenant.id == tenant_id).one_or_404()

        return tenant.custom_config_dict


class RegisterService:

    @classmethod
    def _get_invitation_token_key(cls, token: str) -> str:
        return f'member_invite:token:{token}'

    @classmethod
    def register(cls, email, name, password: str = None, open_id: str = None, provider: str = None) -> Account:
        db.session.begin_nested()
        """Register account"""
        try:
            account = AccountService.create_account(email, name, interface_language=languages[0], password=password)
            account.status = AccountStatus.ACTIVE.value
            account.initialized_at = datetime.utcnow()

            if open_id is not None or provider is not None:
                AccountService.link_account_integrate(provider, open_id, account)

            tenant = TenantService.create_tenant(f"{account.name}'s Workspace")

            TenantService.create_tenant_member(tenant, account, role='owner')
            account.current_tenant = tenant

            db.session.commit()
        except Exception as e:
            db.session.rollback()  # todo: do not work
            logging.error(f'Register failed: {e}')
            raise AccountRegisterError(f'Registration failed: {e}') from e

        tenant_was_created.send(tenant)

        return account

    @classmethod
    def invite_new_member(cls, tenant: Tenant, email: str, language: str, role: str = 'normal', inviter: Account = None) -> str:    
        """Invite new member"""
        account = Account.query.filter_by(email=email).first()

        if not account:
            TenantService.check_member_permission(tenant, inviter, None, 'add')
            name = email.split('@')[0]
            account = AccountService.create_account(email, name, interface_language=language)
            account.status = AccountStatus.PENDING.value
            db.session.commit()

            TenantService.create_tenant_member(tenant, account, role)
        else:
            TenantService.check_member_permission(tenant, inviter, account, 'add')
            ta = TenantAccountJoin.query.filter_by(
                tenant_id=tenant.id,
                account_id=account.id
            ).first()

            if not ta:
                TenantService.create_tenant_member(tenant, account, role)

            # Support resend invitation email when the account is pending status
            if account.status != AccountStatus.PENDING.value:
                raise AccountAlreadyInTenantError("Account already in tenant.")

        token = cls.generate_invite_token(tenant, account)

        # send email
        send_invite_member_mail_task.delay(
            language=account.interface_language,
            to=email,
            token=token,
            inviter_name=inviter.name if inviter else 'Dify',
            workspace_name=tenant.name,
        )

        return token

    @classmethod
    def generate_invite_token(cls, tenant: Tenant, account: Account) -> str:
        token = str(uuid.uuid4())
        invitation_data = {
            'account_id': account.id,
            'email': account.email,
            'workspace_id': tenant.id,
        }
        expiryHours = current_app.config['INVITE_EXPIRY_HOURS']
        redis_client.setex(
            cls._get_invitation_token_key(token),
            expiryHours * 60 * 60,
            json.dumps(invitation_data)
        )
        return token

    @classmethod
    def revoke_token(cls, workspace_id: str, email: str, token: str):
        if workspace_id and email:
            email_hash = sha256(email.encode()).hexdigest()
            cache_key = 'member_invite_token:{}, {}:{}'.format(workspace_id, email_hash, token)
            redis_client.delete(cache_key)
        else:
            redis_client.delete(cls._get_invitation_token_key(token))

    @classmethod
    def get_invitation_if_token_valid(cls, workspace_id: str, email: str, token: str) -> Optional[Dict[str, Any]]:
        invitation_data = cls._get_invitation_by_token(token, workspace_id, email)
        if not invitation_data:
            return None

        tenant = db.session.query(Tenant).filter(
            Tenant.id == invitation_data['workspace_id'],
            Tenant.status == 'normal'
        ).first()

        if not tenant:
            return None

        tenant_account = db.session.query(Account, TenantAccountJoin.role).join(
            TenantAccountJoin, Account.id == TenantAccountJoin.account_id
        ).filter(Account.email == invitation_data['email'], TenantAccountJoin.tenant_id == tenant.id).first()

        if not tenant_account:
            return None

        account = tenant_account[0]
        if not account:
            return None

        if invitation_data['account_id'] != str(account.id):
            return None

        return {
            'account': account,
            'data': invitation_data,
            'tenant': tenant,
        }

    @classmethod
    def _get_invitation_by_token(cls, token: str, workspace_id: str, email: str) -> Optional[Dict[str, str]]:
        if workspace_id is not None and email is not None:
            email_hash = sha256(email.encode()).hexdigest()
            cache_key = f'member_invite_token:{workspace_id}, {email_hash}:{token}'
            account_id = redis_client.get(cache_key)

            if not account_id:
                return None

            return {
                'account_id': account_id.decode('utf-8'),
                'email': email,
                'workspace_id': workspace_id,
            }
        else:
            data = redis_client.get(cls._get_invitation_token_key(token))
            if not data:
                return None

            invitation = json.loads(data)
            return invitation
