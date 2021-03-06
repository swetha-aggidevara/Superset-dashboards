# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=C,R,W
"""A set of constants and methods to manage permissions and security"""
import logging
from os import isatty
from typing import Callable, List, Optional, Set, Tuple, TYPE_CHECKING, Union
import urllib

from flask import current_app, g
from flask_appbuilder import Model
from flask_appbuilder.security.sqla import models as ab_models
from flask_appbuilder.security.sqla.manager import SecurityManager
from flask_appbuilder.security.views import (
    PermissionModelView,
    PermissionViewModelView,
    RoleModelView,
    UserModelView,
)
from flask_appbuilder.widgets import ListWidget
from sqlalchemy import or_
from sqlalchemy.engine import create_engine
from sqlalchemy.engine.base import Connection
from sqlalchemy.orm.mapper import Mapper
from sqlalchemy.orm.session import sessionmaker
from flask_appbuilder.security.sqla.models import User

from superset import sql_parse
from superset.connectors.connector_registry import ConnectorRegistry
from superset.exceptions import SupersetSecurityException
from superset.utils.core import DatasourceName

import json
import requests
from flask import redirect, g, flash, request, session
from flask_appbuilder.security.views import UserDBModelView, AuthDBView, expose
from flask_appbuilder.security.manager import BaseSecurityManager
from flask_login import login_user, logout_user

if TYPE_CHECKING:
    from superset.common.query_context import QueryContext
    from superset.connectors.base.models import BaseDatasource
    from superset.models.core import Database
    from superset.viz import BaseViz


class SupersetSecurityListWidget(ListWidget):
    """
        Redeclaring to avoid circular imports
    """

    template = "superset/fab_overrides/list.html"


class SupersetRoleListWidget(ListWidget):
    """
        Role model view from FAB already uses a custom list widget override
        So we override the override
    """

    template = "superset/fab_overrides/list_role.html"

    def __init__(self, **kwargs):
        kwargs["appbuilder"] = current_app.appbuilder
        super().__init__(**kwargs)


UserModelView.list_widget = SupersetSecurityListWidget
RoleModelView.list_widget = SupersetRoleListWidget
PermissionViewModelView.list_widget = SupersetSecurityListWidget
PermissionModelView.list_widget = SupersetSecurityListWidget


class SupersetSecurityManager(SecurityManager):
    READ_ONLY_MODEL_VIEWS = {"DatabaseAsync", "DatabaseView", "DruidClusterModelView"}

    USER_MODEL_VIEWS = {
        "UserDBModelView",
        "UserLDAPModelView",
        "UserOAuthModelView",
        "UserOIDModelView",
        "UserRemoteUserModelView",
    }

    GAMMA_READ_ONLY_MODEL_VIEWS = {
        "SqlMetricInlineView",
        "TableColumnInlineView",
        "TableModelView",
        "DruidColumnInlineView",
        "DruidDatasourceModelView",
        "DruidMetricInlineView",
    } | READ_ONLY_MODEL_VIEWS

    ADMIN_ONLY_VIEW_MENUS = {
        "AccessRequestsModelView",
        "Manage",
        "SQL Lab",
        "Queries",
        "Refresh Druid Metadata",
        "ResetPasswordView",
        "RoleModelView",
        "Security",
    } | USER_MODEL_VIEWS

    ALPHA_ONLY_VIEW_MENUS = {"Upload a CSV"}

    ADMIN_ONLY_PERMISSIONS = {
        "can_sql_json",  # TODO: move can_sql_json to sql_lab role
        "can_override_role_permissions",
        "can_sync_druid_source",
        "can_override_role_permissions",
        "can_approve",
        "can_update_role",
    }

    READ_ONLY_PERMISSION = {"can_show", "can_list"}

    ALPHA_ONLY_PERMISSIONS = {
        "muldelete",
        "all_database_access",
        "all_datasource_access",
    }

    OBJECT_SPEC_PERMISSIONS = {
        "database_access",
        "schema_access",
        "datasource_access",
        "metric_access",
        "can_only_access_owned_queries",
    }

    ACCESSIBLE_PERMS = {"can_userinfo"}

    def get_schema_perm(
        self, database: Union["Database", str], schema: Optional[str] = None
    ) -> Optional[str]:
        """
        Return the database specific schema permission.

        :param database: The Superset database or database name
        :param schema: The Superset schema name
        :return: The database specific schema permission
        """

        if schema:
            return f"[{database}].[{schema}]"

        return None

    def can_access(self, permission_name: str, view_name: str) -> bool:
        """
        Return True if the user can access the FAB permission/view, False
        otherwise.

        Note this method adds protection from has_access failing from missing
        permission/view entries.

        :param permission_name: The FAB permission name
        :param view_name: The FAB view-menu name
        :returns: Whether the use can access the FAB permission/view
        """

        user = g.user
        if user.is_anonymous:
            return self.is_item_public(permission_name, view_name)
        return self._has_view_access(user, permission_name, view_name)

    def can_only_access_owned_queries(self) -> bool:
        """
        Return True if the user can only access owned queries, False otherwise.

        :returns: Whether the use can only access owned queries
        """
        return self.can_access(
            "can_only_access_owned_queries", "can_only_access_owned_queries"
        )

    def all_datasource_access(self) -> bool:
        """
        Return True if the user can access all Superset datasources, False otherwise.

        :returns: Whether the user can access all Superset datasources
        """

        return self.can_access("all_datasource_access", "all_datasource_access")

    def all_database_access(self) -> bool:
        """
        Return True if the user can access all Superset databases, False otherwise.

        :returns: Whether the user can access all Superset databases
        """

        return self.can_access("all_database_access", "all_database_access")

    def database_access(self, database: "Database") -> bool:
        """
        Return True if the user can access the Superset database, False otherwise.

        :param database: The Superset database
        :returns: Whether the user can access the Superset database
        """
        return (
            self.all_datasource_access()
            or self.all_database_access()
            or self.can_access("database_access", database.perm)
        )

    def schema_access(self, datasource: "BaseDatasource") -> bool:
        """
        Return True if the user can access the schema associated with the Superset
        datasource, False otherwise.

        Note for Druid datasources the database and schema are akin to the Druid cluster
        and datasource name prefix, i.e., [schema.]datasource, respectively.

        :param datasource: The Superset datasource
        :returns: Whether the user can access the datasource's schema
        """

        return (
            self.all_datasource_access()
            or self.database_access(datasource.database)
            or self.can_access("schema_access", datasource.schema_perm)
        )

    def datasource_access(self, datasource: "BaseDatasource") -> bool:
        """
        Return True if the user can access the Superset datasource, False otherwise.

        :param datasource: The Superset datasource
        :returns: Whether the use can access the Superset datasource
        """

        return self.schema_access(datasource) or self.can_access(
            "datasource_access", datasource.perm
        )

    def get_datasource_access_error_msg(self, datasource: "BaseDatasource") -> str:
        """
        Return the error message for the denied Superset datasource.

        :param datasource: The denied Superset datasource
        :returns: The error message
        """

        return f"""This endpoint requires the datasource {datasource.name}, database or
            `all_datasource_access` permission"""

    def get_datasource_access_link(self, datasource: "BaseDatasource") -> Optional[str]:
        """
        Return the link for the denied Superset datasource.

        :param datasource: The denied Superset datasource
        :returns: The access URL
        """

        from superset import conf

        return conf.get("PERMISSION_INSTRUCTIONS_LINK")

    def get_table_access_error_msg(self, tables: List[str]) -> str:
        """
        Return the error message for the denied SQL tables.

        Note the table names conform to the [[cluster.]schema.]table construct.

        :param tables: The list of denied SQL table names
        :returns: The error message
        """
        quoted_tables = [f"`{t}`" for t in tables]
        return f"""You need access to the following tables: {", ".join(quoted_tables)},
            `all_database_access` or `all_datasource_access` permission"""

    def get_table_access_link(self, tables: List[str]) -> Optional[str]:
        """
        Return the access link for the denied SQL tables.

        Note the table names conform to the [[cluster.]schema.]table construct.

        :param tables: The list of denied SQL table names
        :returns: The access URL
        """

        from superset import conf

        return conf.get("PERMISSION_INSTRUCTIONS_LINK")

    def _datasource_access_by_name(
        self, database: "Database", table_name: str, schema: str = None
    ) -> bool:
        """
        Return True if the user can access the SQL table, False otherwise.

        :param database: The SQL database
        :param table_name: The SQL table name
        :param schema: The Superset schema
        :returns: Whether the use can access the SQL table
        """

        from superset import db

        if self.database_access(database) or self.all_datasource_access():
            return True

        schema_perm = self.get_schema_perm(database, schema)
        if schema_perm and self.can_access("schema_access", schema_perm):
            return True

        datasources = ConnectorRegistry.query_datasources_by_name(
            db.session, database, table_name, schema=schema
        )
        for datasource in datasources:
            if self.can_access("datasource_access", datasource.perm):
                return True
        return False

    def _get_schema_and_table(
        self, table_in_query: str, schema: str
    ) -> Tuple[str, str]:
        """
        Return the SQL schema/table tuple associated with the table extracted from the
        SQL query.

        Note the table name conforms to the [[cluster.]schema.]table construct.

        :param table_in_query: The SQL table name
        :param schema: The fallback SQL schema if not present in the table name
        :returns: The SQL schema/table tuple
        """

        table_name_pieces = table_in_query.split(".")
        if len(table_name_pieces) == 3:
            return tuple(table_name_pieces[1:])  # type: ignore
        elif len(table_name_pieces) == 2:
            return tuple(table_name_pieces)  # type: ignore
        return (schema, table_name_pieces[0])

    def _datasource_access_by_fullname(
        self, database: "Database", table_in_query: str, schema: str
    ) -> bool:
        """
        Return True if the user can access the table extracted from the SQL query, False
        otherwise.

        Note the table name conforms to the [[cluster.]schema.]table construct.

        :param database: The Superset database
        :param table_in_query: The SQL table name
        :param schema: The fallback SQL schema, i.e., if not present in the table name
        :returns: Whether the user can access the SQL table
        """

        table_schema, table_name = self._get_schema_and_table(table_in_query, schema)
        return self._datasource_access_by_name(
            database, table_name, schema=table_schema
        )

    def rejected_tables(self, sql: str, database: "Database", schema: str) -> List[str]:
        """
        Return the list of rejected SQL table names.

        Note the rejected table names conform to the [[cluster.]schema.]table construct.

        :param sql: The SQL statement
        :param database: The SQL database
        :param schema: The SQL database schema
        :returns: The rejected table names
        """

        superset_query = sql_parse.ParsedQuery(sql)

        return [
            t
            for t in superset_query.tables
            if not self._datasource_access_by_fullname(database, t, schema)
        ]

    def _user_datasource_perms(self) -> Set[str]:
        """
        Return the set of FAB permission view-menu names the user can access.

        :returns: The set of FAB permission view-menu names
        """

        datasource_perms = set()
        for r in g.user.roles:
            for perm in r.permissions:
                if perm.permission and "datasource_access" == perm.permission.name:
                    datasource_perms.add(perm.view_menu.name)
        return datasource_perms

    def schemas_accessible_by_user(
        self, database: "Database", schemas: List[str], hierarchical: bool = True
    ) -> List[str]:
        """
        Return the sorted list of SQL schemas accessible by the user.

        :param database: The SQL database
        :param schemas: The list of eligible SQL schemas
        :param hierarchical: Whether to check using the hierarchical permission logic
        :returns: The list of accessible SQL schemas
        """

        from superset import db
        from superset.connectors.sqla.models import SqlaTable

        if hierarchical and (
            self.database_access(database) or self.all_datasource_access()
        ):
            return schemas

        subset = set()
        for schema in schemas:
            schema_perm = self.get_schema_perm(database, schema)
            if schema_perm and self.can_access("schema_access", schema_perm):
                subset.add(schema)

        perms = self._user_datasource_perms()
        if perms:
            tables = (
                db.session.query(SqlaTable)
                .filter(SqlaTable.perm.in_(perms), SqlaTable.database_id == database.id)
                .all()
            )
            for t in tables:
                if t.schema:
                    subset.add(t.schema)
        return sorted(list(subset))

    def get_datasources_accessible_by_user(
        self,
        database: "Database",
        datasource_names: List[DatasourceName],
        schema: Optional[str] = None,
    ) -> List[DatasourceName]:
        """
        Return the list of SQL tables accessible by the user.

        :param database: The SQL database
        :param datasource_names: The list of eligible SQL tables w/ schema
        :param schema: The fallback SQL schema if not present in the table name
        :returns: The list of accessible SQL tables w/ schema
        """

        from superset import db

        if self.database_access(database) or self.all_datasource_access():
            return datasource_names

        if schema:
            schema_perm = self.get_schema_perm(database, schema)
            if schema_perm and self.can_access("schema_access", schema_perm):
                return datasource_names

        user_perms = self._user_datasource_perms()
        user_datasources = ConnectorRegistry.query_datasources_by_permissions(
            db.session, database, user_perms
        )
        if schema:
            names = {d.table_name for d in user_datasources if d.schema == schema}
            return [d for d in datasource_names if d in names]
        else:
            full_names = {d.full_name for d in user_datasources}
            return [d for d in datasource_names if d in full_names]

    def merge_perm(self, permission_name: str, view_menu_name: str) -> None:
        """
        Add the FAB permission/view-menu.

        :param permission_name: The FAB permission name
        :param view_menu_names: The FAB view-menu name
        :see: SecurityManager.add_permission_view_menu
        """

        logging.warning(
            "This method 'merge_perm' is deprecated use add_permission_view_menu"
        )
        self.add_permission_view_menu(permission_name, view_menu_name)

    def _is_user_defined_permission(self, perm: Model) -> bool:
        """
        Return True if the FAB permission is user defined, False otherwise.

        :param perm: The FAB permission
        :returns: Whether the FAB permission is user defined
        """

        return perm.permission.name in self.OBJECT_SPEC_PERMISSIONS

    def create_custom_permissions(self) -> None:
        """
        Create custom FAB permissions.
        """

        self.add_permission_view_menu("all_datasource_access", "all_datasource_access")
        self.add_permission_view_menu("all_database_access", "all_database_access")
        self.add_permission_view_menu(
            "can_only_access_owned_queries", "can_only_access_owned_queries"
        )

    def create_missing_perms(self) -> None:
        """
        Creates missing FAB permissions for datasources, schemas and metrics.
        """

        from superset import db
        from superset.connectors.base.models import BaseMetric
        from superset.models import core as models

        logging.info("Fetching a set of all perms to lookup which ones are missing")
        all_pvs = set()
        for pv in self.get_session.query(self.permissionview_model).all():
            if pv.permission and pv.view_menu:
                all_pvs.add((pv.permission.name, pv.view_menu.name))

        def merge_pv(view_menu, perm):
            """Create permission view menu only if it doesn't exist"""
            if view_menu and perm and (view_menu, perm) not in all_pvs:
                self.add_permission_view_menu(view_menu, perm)

        logging.info("Creating missing datasource permissions.")
        datasources = ConnectorRegistry.get_all_datasources(db.session)
        for datasource in datasources:
            merge_pv("datasource_access", datasource.get_perm())
            merge_pv("schema_access", datasource.schema_perm)

        logging.info("Creating missing database permissions.")
        databases = db.session.query(models.Database).all()
        for database in databases:
            merge_pv("database_access", database.perm)

        logging.info("Creating missing metrics permissions")
        metrics: List[BaseMetric] = []
        for datasource_class in ConnectorRegistry.sources.values():
            metrics += list(db.session.query(datasource_class.metric_class).all())

    def clean_perms(self) -> None:
        """
        Clean up the FAB faulty permissions.
        """

        logging.info("Cleaning faulty perms")
        sesh = self.get_session
        pvms = sesh.query(ab_models.PermissionView).filter(
            or_(
                ab_models.PermissionView.permission == None,
                ab_models.PermissionView.view_menu == None,
            )
        )
        deleted_count = pvms.delete()
        sesh.commit()
        if deleted_count:
            logging.info("Deleted {} faulty permissions".format(deleted_count))

    def sync_role_definitions(self) -> None:
        """
        Initialize the Superset application with security roles and such.
        """

        from superset import conf

        logging.info("Syncing role definition")

        self.create_custom_permissions()

        # Creating default roles
        self.set_role("Admin", self._is_admin_pvm)
        self.set_role("Alpha", self._is_alpha_pvm)
        self.set_role("Gamma", self._is_gamma_pvm)
        self.set_role("granter", self._is_granter_pvm)
        self.set_role("sql_lab", self._is_sql_lab_pvm)

        if conf.get("PUBLIC_ROLE_LIKE_GAMMA", False):
            self.set_role("Public", self._is_gamma_pvm)

        self.create_missing_perms()

        # commit role and view menu updates
        self.get_session.commit()
        self.clean_perms()

    def set_role(self, role_name: str, pvm_check: Callable) -> None:
        """
        Set the FAB permission/views for the role.

        :param role_name: The FAB role name
        :param pvm_check: The FAB permission/view check
        """

        logging.info("Syncing {} perms".format(role_name))
        sesh = self.get_session
        pvms = sesh.query(ab_models.PermissionView).all()
        pvms = [p for p in pvms if p.permission and p.view_menu]
        role = self.add_role(role_name)
        role_pvms = [p for p in pvms if pvm_check(p)]
        role.permissions = role_pvms
        sesh.merge(role)
        sesh.commit()

    def _is_admin_only(self, pvm: Model) -> bool:
        """
        Return True if the FAB permission/view is accessible to only Admin users,
        False otherwise.

        Note readonly operations on read only model views are allowed only for admins.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to only Admin users
        """

        if (
            pvm.view_menu.name in self.READ_ONLY_MODEL_VIEWS
            and pvm.permission.name not in self.READ_ONLY_PERMISSION
        ):
            return True
        return (
            pvm.view_menu.name in self.ADMIN_ONLY_VIEW_MENUS
            or pvm.permission.name in self.ADMIN_ONLY_PERMISSIONS
        )

    def _is_alpha_only(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is accessible to only Alpha users,
        False otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to only Alpha users
        """

        if (
            pvm.view_menu.name in self.GAMMA_READ_ONLY_MODEL_VIEWS
            and pvm.permission.name not in self.READ_ONLY_PERMISSION
        ):
            return True
        return (
            pvm.view_menu.name in self.ALPHA_ONLY_VIEW_MENUS
            or pvm.permission.name in self.ALPHA_ONLY_PERMISSIONS
        )

    def _is_accessible_to_all(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is accessible to all, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is accessible to all users
        """

        return pvm.permission.name in self.ACCESSIBLE_PERMS

    def _is_admin_pvm(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is Admin user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Admin related
        """

        return not self._is_user_defined_permission(pvm)

    def _is_alpha_pvm(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is Alpha user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Alpha related
        """

        return not (
            self._is_user_defined_permission(pvm) or self._is_admin_only(pvm)
        ) or self._is_accessible_to_all(pvm)

    def _is_gamma_pvm(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is Gamma user related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is Gamma related
        """

        return not (
            self._is_user_defined_permission(pvm)
            or self._is_admin_only(pvm)
            or self._is_alpha_only(pvm)
        ) or self._is_accessible_to_all(pvm)

    def _is_sql_lab_pvm(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the FAB permission/view is SQL Lab related, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the FAB object is SQL Lab related
        """

        return (
            pvm.view_menu.name
            in {"SQL Lab", "SQL Editor", "Query Search", "Saved Queries"}
            or pvm.permission.name
            in {
                "can_sql_json",
                "can_csv",
                "can_search_queries",
                "can_sqllab_viz",
                "can_sqllab",
            }
            or (
                pvm.view_menu.name in self.USER_MODEL_VIEWS
                and pvm.permission.name == "can_list"
            )
        )

    def _is_granter_pvm(self, pvm: PermissionModelView) -> bool:
        """
        Return True if the user can grant the FAB permission/view, False
        otherwise.

        :param pvm: The FAB permission/view
        :returns: Whether the user can grant the FAB permission/view
        """

        return pvm.permission.name in {"can_override_role_permissions", "can_approve"}

    def set_perm(
        self, mapper: Mapper, connection: Connection, target: "BaseDatasource"
    ) -> None:
        """
        Set the datasource permissions.

        :param mapper: The table mappper
        :param connection: The DB-API connection
        :param target: The mapped instance being persisted
        """

        if target.perm != target.get_perm():
            link_table = target.__table__
            connection.execute(
                link_table.update()
                .where(link_table.c.id == target.id)
                .values(perm=target.get_perm())
            )

        # add to view menu if not already exists
        permission_name = "datasource_access"
        view_menu_name = target.get_perm()
        permission = self.find_permission(permission_name)
        view_menu = self.find_view_menu(view_menu_name)
        pv = None

        if not permission:
            permission_table = (
                self.permission_model.__table__  # pylint: disable=no-member
            )
            connection.execute(permission_table.insert().values(name=permission_name))
            permission = self.find_permission(permission_name)
        if not view_menu:
            view_menu_table = self.viewmenu_model.__table__  # pylint: disable=no-member
            connection.execute(view_menu_table.insert().values(name=view_menu_name))
            view_menu = self.find_view_menu(view_menu_name)

        if permission and view_menu:
            pv = (
                self.get_session.query(self.permissionview_model)
                .filter_by(permission=permission, view_menu=view_menu)
                .first()
            )
        if not pv and permission and view_menu:
            permission_view_table = (
                self.permissionview_model.__table__  # pylint: disable=no-member
            )
            connection.execute(
                permission_view_table.insert().values(
                    permission_id=permission.id, view_menu_id=view_menu.id
                )
            )

    def assert_datasource_permission(self, datasource: "BaseDatasource") -> None:
        """
        Assert the the user has permission to access the Superset datasource.

        :param datasource: The Superset datasource
        :raises SupersetSecurityException: If the user does not have permission
        """

        if not self.datasource_access(datasource):
            raise SupersetSecurityException(
                self.get_datasource_access_error_msg(datasource),
                self.get_datasource_access_link(datasource),
            )

    def assert_query_context_permission(self, query_context: "QueryContext") -> None:
        """
        Assert the the user has permission to access the query context.

        :param query_context: The query context
        :raises SupersetSecurityException: If the user does not have permission
        """

        self.assert_datasource_permission(query_context.datasource)

    def assert_viz_permission(self, viz: "BaseViz") -> None:
        """
        Assert the the user has permission to access the visualization.

        :param viz: The visualization
        :raises SupersetSecurityException: If the user does not have permission
        """

        self.assert_datasource_permission(viz.datasource)


# Custom Security for user login by passing parameters
global myObj
from superset import security
import requests
import datetime

class CustomAuthDBView(AuthDBView):
    login_template = "appbuilder/general/security/login_db.html"
    userToLogIn = None
    isValid = False
    userObj = None
    programs = None
    programNames = None
    token = None
    country = 'No Country'
    jwt_token = None
    isAdmin = False
    isProgramAdmin = False
    userId = None

    # function for searching an element in a list/array
    def search(self, list, text):
        for i in range(len(list)):
            if text.__contains__(list[i]):
                return True
        return False
    

    @expose("/getCountry")
    def getCountry(self):
        return {"response":'OK',"country":session.get('country','No Country')}


    @expose("/getToken", methods=["GET", "POST"])
    def getToken(self): 
        return {'res':'self.jwt_token'}

    @expose("/setToken", methods=["GET", "POST"])
    def setToken(self):
        import jwt
        token = json.loads(request.data)["request"]["token"]
        decoded = jwt.decode(token,verify=False, algorithms='HS264')
        userId = decoded['sub']
        roles = decoded['realm_access']['roles']
        programNames = None
        userPrograms = None
        requestObjectForPrograms = {
            "request":{
                "userId":userId,
                "token":token
            }
        }
        requestForPrograms = requests.post("http://localhost:8000/supersetdashboards/api/v1/program/user", json=requestObjectForPrograms)
        requestForuserDetails = requests.post("http://localhost:8000/supersetdashboards/api/v1/user-details", json=requestObjectForPrograms)
        userDetails = requestForuserDetails.json()
        programNames=requestForPrograms.json()['programNames']
        userPrograms=requestForPrograms.json()['userPrograms']
        country = userDetails["country"]
        isAdmin = self.search(roles,'admin')
        isProgramAdmin = self.search(roles,'admin') == False
        self.isAdmin = isAdmin
        self.isProgramAdmin = isProgramAdmin
        self.userId = userId
        self.pdaUser = requestForPrograms.json()
        self.programs = userPrograms
        self.programNames = programNames
        self.country = country
        print("##############################",programNames,userPrograms,userId,roles,country)
        return {'response':{'admin':isAdmin,'programAdmin':isProgramAdmin,'country':country,'programs':userPrograms,'programNames':programNames}}

    # api to get encrypted parameters to be used for login use
    @expose("/handleLogin", methods=["GET", "POST"])
    def handleLogin(self):
        from superset import jinja_context
        req = {
            "request": {"cipherText": json.loads(request.data)["request"]["encrypted"]}
        }

        # to decrypt encrypted parameters
        r = requests.post("http://localhost:8000/supersetdashboards/api/v1/decrypt", json=req)
        self.userObj = r.json()["response"]["decryptedObject"]
        iatObj = r.json()["response"]["decryptedObject"]["iat"]
        self.programs = r.json()["response"]["decryptedObject"]["programs"]
        self.programNames = r.json()["response"]["decryptedObject"]["programNames"]
        self.token = r.json()["response"]["decryptedObject"]["token"]
        jinja_context.BASE_CONTEXT['token'] = self.token

        req2 = {"request":{"token":r.json()["response"]["decryptedObject"]["token"]}}
        s = requests.post("http://localhost:8000/supersetdashboards/api/v1/user-details", json=req2)
        self.country = s.json()['country']

        dateFromReq = datetime.datetime(
            iatObj["year"],
            iatObj["month"] + 1,
            iatObj["day"],
            iatObj["hour"],
            iatObj["minutes"],
            iatObj["seconds"],
            iatObj["milis"],
        )
        currentDate = datetime.datetime.utcnow()

        # check validity of token and redirect url
        self.isValid = (
            0 <= (dateFromReq - currentDate + datetime.timedelta(minutes=30)).days
        ) and (
            request.headers.get("Referer") is not None
            and self.search(
                current_app.config.get("VALID_REFERER_URLS"),
                request.headers.get("Referer"),
            )
        )
        return {
            "response": "OK",
            "encrypted": json.loads(request.data)["request"]["encrypted"],
        }
    
    @expose("/superset/pdaUserDetails", methods=["GET", "POST"])
    def pdaUserDetails(self):
        if self.pdaUser is not None:
            response = self.pdaUser
        else:
            response = {}
        return (response)
    
    @expose("/superset/login", methods=["GET", "POST"])
    def login(self):
        print("########CALLED##########################################")
        from superset import jinja_context
        redirect_url = self.appbuilder.get_url_for_index
        sample_url = "/superset/dashboard/7/"
        role = None
        userId = self.userId
        # self.set_userId("userId", self.userId)
        programs = self.programs
        programNames = self.programNames
        isAdmin = self.isAdmin
        isProgramAdmin = self.isProgramAdmin
        dashboard = request.args.get("dashboard",None)
        isValidReferer = request.headers.get("Referer") is not None and self.search(
            current_app.config.get("VALID_REFERER_URLS"), request.headers.get("Referer")
        )
        if programs is not None and len(programs) ==1 and len(programNames) == 1:
            programs.append(programs[0])
            programNames.append(programNames[0])

        else:
            pass

        print("#############################"," contains /superset/login-> ",request.headers.get("Referer")," IsvalidReferer->",isValidReferer," REferer->",request.headers.get("Referer")," Valid referers->",current_app.config.get("VALID_REFERER_URLS"))

        if dashboard is not None:
            redirect_url =  "/superset/dashboard/"+ dashboard

        if isValidReferer == True and request.headers.get("Referer") is not None and request.headers.get("Referer").__contains__('/superset/login') == False:
            try:
                #resObj = self.userObj
                #role = resObj["role"]
                #userId = resObj["userId"]
                self.userToLogIn = "public_user" # check for username in instance

                """if isProgramAdmin == True:
                    self.userToLogIn = "public_user"
                    role = 'Program Admin'

                elif isAdmin == True:
                    self.userToLogIn = "admin"
                    role = 'Admin'

                else:
                    pass"""

            except:
                pass

            user = self.appbuilder.sm.find_user(username=self.userToLogIn)
            if user is None:
                return {"error":"please retry login"}
            else:
                pass
            login_user(user, remember=False)
            #add additional information in session
            #session["userId"] = userId
            #session["role"] = role
            session["referer"] = request.headers.get("Referer")
            session['programs'] = programs
            session['programNames'] = programNames
            session['country'] = self.country

 # get superset username by id and store it in session
            user_id=session.get('user_id',None)
            user_name_superset=None
            if user_id is not None:
                user_id=int(user_id)
            #for getting dbURL for SQLITE
            sqlite_engine=create_engine(current_app.config.get("SQLALCHEMY_DATABASE_URI"))
            #create session for querying sqlite db
            SessionForSqlite = sessionmaker(bind=sqlite_engine)
            s1= SessionForSqlite()
            #get userinfo
            #user_name_superset
            userInfo=s1.query(User).filter(User.id==user_id).all()
            for r in userInfo:
                user_name_superset=r.username
            session['user_name_superset']=user_name_superset
            session['userName']=user_name_superset

            s1.commit()
            s1.close()
            print("###########################################################",session.get('user_name_superset'))
        
            return redirect(redirect_url)
        else:
            flash("Unable to auto login", "warning")
            self.pdaUser = {}
            #return 'Invalid Url'
            return super(CustomAuthDBView, self).login()
            #return redirect('/')

    @expose("/logout/", methods=["GET", "POST"])
    def logout(self):
        import urllib
        logout_url: str = "/superset/login"
        ssologouturl = "http://localhost:4200/supersetdashboards/ssologout"
        pdaUrl = current_app.config.get('PDA_URL')
        logout_user()
        return redirect(ssologouturl)
        pdaUrl = current_app.config.get('PDA_URL') #check for host for every deployment
        pdaLoginPageUrl = current_app.config.get('PDA_LOGIN_URL')
      
        if session.get("referer", None) is not None and session.get(
            "referer"
        ).__contains__(pdaUrl):
            # logout url is pda login page
            scheme=urllib.parse.urlparse(session.get("referer")).scheme
            netloc=urllib.parse.urlparse(session.get("referer")).netloc
            logout_url=scheme+'://'+ netloc +'/'+'supersetdashboards/ssologout'
            print("############################################ IN IF",logout_url)

        elif session.get("referer", None) is not None and urllib.parse.urlparse(session.get("referer")).port is not None:
            scheme=urllib.parse.urlparse(session.get("referer")).scheme
            netloc=urllib.parse.urlparse(session.get("referer")).netloc
            logout_url=scheme+'://'+ netloc +'/'+'supersetdashboards/ssologout'
        else:
            pass
        print("########LOGOUT URL### ",logout_url)

        # return redirect("/")
        session.pop('referer',None)
        session.pop('userName',None)
        session.pop('programs',None)
        session.pop('programNames',None)
        logout_user()

        return redirect(logout_url)


class CustomSecurityManager(SupersetSecurityManager):

    def showProfile(self):
        import os
        user_id=session.get('user_id',None)
        user_name_superset=''
        usernames=os.environ.get('usernames',"public_user,pdauser").split(",")
        if user_id is not None:
            user_id=int(user_id)
        #for getting dbURL for SQLITE
        sqlite_engine=create_engine(current_app.config.get("SQLALCHEMY_DATABASE_URI"))

        SessionForSqlite = sessionmaker(bind=sqlite_engine)
        s1= SessionForSqlite()
   
        userInfo=s1.query(User).filter(User.id==user_id).all()
        for r in userInfo:
            user_name_superset=r.username
       
        s1.commit()
        s1.close()
        
        return not user_name_superset in usernames

    authdbview = CustomAuthDBView

    def __init__(self, appbuilder):
        super(CustomSecurityManager, self).__init__(appbuilder)
