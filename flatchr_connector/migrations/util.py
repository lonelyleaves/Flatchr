# Utility functions for migration scripts

import base64
import collections
import datetime
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from functools import reduce
from inspect import currentframe
from itertools import chain, islice
from multiprocessing import cpu_count
from operator import itemgetter
from textwrap import dedent

import lxml
import markdown
import psycopg2
from docutils.core import publish_string

try:
    import odoo
    from odoo import SUPERUSER_ID, release
except ImportError:
    import openerp as odoo
    from openerp import SUPERUSER_ID, release

try:
    from odoo.addons.base.models.ir_module import MyWriter  # > 11.0
except ImportError:
    try:
        from odoo.addons.base.module.module import MyWriter
    except ImportError:
        from openerp.addons.base.module.module import MyWriter

try:
    from odoo.modules.module import (
        get_module_path,
        load_information_from_description_file,
    )
    from odoo.sql_db import db_connect
    from odoo.tools import UnquoteEvalContext
    from odoo.tools.convert import xml_import
    from odoo.tools.func import frame_codeinfo
    from odoo.tools.mail import html_sanitize
    from odoo.tools.misc import file_open
    from odoo.tools.parse_version import parse_version
    from odoo.tools.safe_eval import safe_eval
except ImportError:
    from openerp.modules.module import (
        get_module_path,
        load_information_from_description_file,
    )
    from openerp.sql_db import db_connect
    from openerp.tools import UnquoteEvalContext
    from openerp.tools.convert import xml_import
    from openerp.tools.func import frame_codeinfo
    from openerp.tools.mail import html_sanitize
    from openerp.tools.misc import file_open
    from openerp.tools.parse_version import parse_version
    from openerp.tools.safe_eval import safe_eval

try:
    from odoo.api import Environment

    manage_env = Environment.manage
except ImportError:
    try:
        from openerp.api import Environment

        manage_env = Environment.manage
    except ImportError:

        @contextmanager
        def manage_env():
            yield


try:
    from concurrent.futures import ThreadPoolExecutor
except ImportError:
    ThreadPoolExecutor = None

_logger = logging.getLogger(__name__)

_INSTALLED_MODULE_STATES = ("installed", "to install", "to upgrade")

# migration environ, used to share data between scripts
ENVIRON = {
    "__renamed_fields": collections.defaultdict(set),
}

NEARLYWARN = 25  # (between and info, appear on runbot build page)

# python3 shims
try:
    basestring
except NameError:
    basestring = unicode = str

migration_reports = {}


def add_to_migration_reports(message, category=None):
    if not category:
        category = "Other"
    migration_reports[category] = migration_reports.get(category, [])
    migration_reports[category].append(message)


class MigrationError(Exception):
    pass


class SleepyDeveloperError(ValueError):
    pass


def version_gte(version):
    if "-" in version:
        raise SleepyDeveloperError("version cannot contains dash")
    return parse_version(release.series) >= parse_version(version)


def main(func, version=None):
    """a main() function for scripts"""
    # NOTE: this is not recommanded when the func callback use the ORM as the addon-path is
    # incomplete. Please pipe your script into `odoo shell`.
    # Do not forget to commit the cursor at the end.
    if len(sys.argv) != 2:
        sys.exit("Usage: %s <dbname>" % (sys.argv[0],))
    dbname = sys.argv[1]
    with db_connect(dbname).cursor() as cr, manage_env():
        func(cr, version)


def splitlines(s):
    """yield stripped lines of `s`.
    Skip empty lines
    Remove comments (starts with `#`).
    """
    return (sl for l in s.splitlines() for sl in [l.split("#", 1)[0].strip()] if sl)


def expand_braces(s):
    # expand braces (a la bash)
    # only handle one expension of a 2 parts (because we don't need more)
    r = re.compile(r"(.*){([^},]*?,[^},]*?)}(.*)")
    m = r.search(s)
    if not m:
        raise ValueError("No expansion braces found")
    head, match, tail = m.groups()
    a, b = match.split(",")
    first = head + a + tail
    second = head + b + tail
    if r.search(first):  # as the regexp will match the last expansion, we only need to verify first term
        raise ValueError("Multiple expansion braces found")
    return [first, second]


try:
    import importlib.util

    def import_script(path, name=None):
        if not name:
            name, _ = os.path.splitext(os.path.basename(path))
        full_path = os.path.join(os.path.dirname(__file__), path)
        spec = importlib.util.spec_from_file_location(name, full_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


except ImportError:
    # python2 version
    import imp

    def import_script(path, name=None):
        if not name:
            name, _ = os.path.splitext(os.path.basename(path))
        full_path = os.path.join(os.path.dirname(__file__), path)
        with open(full_path) as fp:
            return imp.load_source(name, full_path, fp)


@contextmanager
def skippable_cm():
    """Allow a contextmanager to not yield."""
    if not hasattr(skippable_cm, "_msg"):

        @contextmanager
        def _():
            if 0:
                yield

        try:
            with _():
                pass
        except RuntimeError as r:
            skippable_cm._msg = str(r)
    try:
        yield
    except RuntimeError as r:
        if str(r) != skippable_cm._msg:
            raise


@contextmanager
def savepoint(cr):
    # NOTE: the `savepoint` method on Cursor only appear in `saas-3`, which mean this function
    #       can't be called when upgrading to saas~1 or saas~2.
    #       I take the bet it won't be problematic...
    with cr.savepoint():
        yield


@contextmanager
def disable_triggers(cr, *tables):
    # NOTE only super user (at pg level) can disable all the triggers. noop if this is not the case.
    if any("." in table for table in tables):
        raise SleepyDeveloperError("table name cannot contains dot")

    cr.execute("SELECT usesuper FROM pg_user WHERE usename = CURRENT_USER")
    is_su = cr.fetchone()[0]

    if is_su:
        for table in tables:
            cr.execute("ALTER TABLE %s DISABLE TRIGGER ALL" % table)

    yield
    if is_su:
        for table in reversed(tables):
            cr.execute("ALTER TABLE %s ENABLE TRIGGER ALL" % table)


def get_max_workers():
    force_max_worker = os.getenv("MAX_WORKER")
    if force_max_worker:
        if not force_max_worker.isdigit():
            raise MigrationError("wrong parameter: MAX_WORKER should be an integer")
        return int(force_max_worker)
    return min(8, cpu_count())


if ThreadPoolExecutor is None:

    def parallel_execute(cr, queries, logger=_logger):
        for query in log_progress(queries, qualifier="queries", logger=logger, size=len(queries)):
            cr.execute(query)


else:

    def parallel_execute(cr, queries, logger=_logger):
        """
        Execute queries in parallel
        Use a maximum of 8 workers (but not more than the number of CPUs)
        Side effect: the given cursor is commited.
        As example, on `next.odoo.com` (using 8 workers), the following gains are:
            +---------------------------------------------+-------------+-------------+
            | File                                        | Sequential  | Parallel    |
            +---------------------------------------------+-------------+-------------+
            | base/saas~12.5.1.3/pre-20-models.py         | ~8 minutes  | ~2 minutes  |
            | mail/saas~12.5.1.0/pre-migrate.py           | ~10 minutes | ~4 minutes  |
            | mass_mailing/saas~12.5.2.0/pre-10-models.py | ~40 minutes | ~18 minutes |
            +---------------------------------------------+-------------+-------------+
        """
        if not queries:
            return
        max_workers = min(get_max_workers(), len(queries))
        reg = env(cr).registry

        def execute(query):
            with reg.cursor() as cr:
                cr.execute(query)
                cr.commit()

        cr.commit()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for _ in log_progress(
                executor.map(execute, queries), qualifier="queries", logger=logger, size=len(queries)
            ):
                pass


def explode_query(cr, query, num_buckets=8, prefix=""):
    """
    Explode a query to multiple queries that can be executed in parallel
    """
    if "{parallel_filter}" not in query:
        sep_kw = " AND " if re.search(r"\sWHERE\s", query, re.M | re.I) else " WHERE "
        query += sep_kw + "{parallel_filter}"

    parallel_filter = "mod(abs({prefix}id), %s) = %s".format(prefix=prefix)
    return [
        cr.mogrify(query.format(parallel_filter=parallel_filter), [num_buckets, index]).decode()
        for index in range(num_buckets)
    ]


def pg_array_uniq(a, drop_null=False):
    dn = "WHERE x IS NOT NULL" if drop_null else ""
    return "ARRAY(SELECT x FROM unnest({}) x {} GROUP BY x)".format(a, dn)


def pg_html_escape(s, quote=True):
    """sql version of html.escape"""
    replacements = [
        ("&", "&amp;"),  # Must be done first!
        ("<", "&lt;"),
        (">", "&gt;"),
    ]
    if quote:
        replacements += [
            ('"', "&quot;"),
            ("'", "&#x27;"),
        ]

    q = lambda s: psycopg2.extensions.QuotedString(s).getquoted().decode("utf-8")  # noqa: E731
    return reduce(lambda s, r: "replace({}, {}, {})".format(s, q(r[0]), q(r[1])), replacements, s)


def pg_text2html(s):
    return r"""
        CASE WHEN TRIM(COALESCE({src}, '')) ~ '^<.+</\w+>$' THEN {src}
             ELSE CONCAT('<p>', replace({esc}, E'\n', '<br>'), '</p>')
         END
    """.format(
        src=s, esc=pg_html_escape(s)
    )


def has_enterprise():
    """Return whernever the current installation has enterprise addons availables"""
    # NOTE should always return True as customers need Enterprise to migrate or
    #      they are on SaaS, which include enterpise addons.
    #      This act as a sanity check for developpers or in case we release the scripts.
    if os.getenv("ODOO_HAS_ENTERPRISE"):
        return True
    # XXX maybe we will need to change this for version > 9
    return bool(get_module_path("delivery_fedex", downloaded=False, display_warning=False))


def is_saas(cr):
    """Return whether the current installation has saas modules installed or not"""
    # this is shitty, I know - but the one above me is as shitty so ¯\_(ツ)_/¯
    cr.execute("SELECT true FROM ir_module_module WHERE name like 'saas_%' AND state='installed'")
    return bool(cr.fetchone())


def dbuuid(cr):
    cr.execute(
        """
        SELECT value
          FROM ir_config_parameter
         WHERE key IN ('database.uuid', 'origin.database.uuid')
      ORDER BY key DESC
         LIMIT 1
    """
    )
    return cr.fetchone()[0]


def dispatch_by_dbuuid(cr, version, callbacks):
    """
    Allow to execute a migration script for a specific database only, base on its dbuuid.
    Example:
    >>> def db_yellowbird(cr, version):
            cr.execute("DELETE FROM ir_ui_view WHERE id=837")
    >>> dispatch_by_dbuuid(cr, version, {
            'ef81c07aa90936a89f4e7878e2ebc634a24fcd66': db_yellowbird,
        })
    """
    uuid = dbuuid(cr)
    if uuid in callbacks:
        func = callbacks[uuid]
        _logger.info("calling dbuuid-specific function `%s`", func.__name__)
        func(cr, version)


IMD_FIELD_PATTERN = "field_%s__%s" if version_gte("saas~11.2") else "field_%s_%s"


def table_of_model(cr, model):
    exceptions = dict(
        l.split()
        for l in splitlines(
            """
        ir.actions.actions          ir_actions
        ir.actions.act_url          ir_act_url
        ir.actions.act_window       ir_act_window
        ir.actions.act_window_close ir_actions
        ir.actions.act_window.view  ir_act_window_view
        ir.actions.client           ir_act_client
        ir.actions.report.xml       ir_act_report_xml
        ir.actions.report           ir_act_report_xml
        ir.actions.server           ir_act_server
        ir.actions.wizard           ir_act_wizard
        stock.picking.in  stock_picking
        stock.picking.out stock_picking
        workflow            wkf
        workflow.activity   wkf_activity
        workflow.instance   wkf_instance
        workflow.transition wkf_transition
        workflow.triggers   wkf_triggers
        workflow.workitem   wkf_workitem
        # mass_mailing
        mail.mass_mailing.list_contact_rel mail_mass_mailing_contact_list_rel
        mailing.contact.subscription       mailing_contact_list_rel
        # `mail.notification` was a "normal" model in versions <9.0
        # and a named m2m in >=saas~13
        {gte_saas13} mail.notification mail_message_res_partner_needaction_rel
    """.format(
                gte_saas13="" if version_gte("9.saas~13") else "#"
            )
        )
    )
    return exceptions.get(model, model.replace(".", "_"))


def model_of_table(cr, table):
    exceptions = dict(
        l.split()
        for l in splitlines(
            """
        # can also be act_window_close, but there are chances it wont be usefull for anyone...
        ir_actions         ir.actions.actions
        ir_act_url         ir.actions.act_url
        ir_act_window      ir.actions.act_window
        ir_act_window_view ir.actions.act_window.view
        ir_act_client      ir.actions.client
        ir_act_report_xml  {action_report_model}
        ir_act_server      ir.actions.server
        ir_act_wizard      ir.actions.wizard
        wkf            workflow
        wkf_activity   workflow.activity
        wkf_instance   workflow.instance
        wkf_transition workflow.transition
        wkf_triggers   workflow.triggers
        wkf_workitem   workflow.workitem
        ir_config_parameter  ir.config_parameter
        documents_request_wizard documents.request_wizard
        survey_user_input survey.user_input
        survey_user_input_line survey.user_input_line
        mail_mass_mailing_contact_list_rel mail.mass_mailing.list_contact_rel
        mailing_contact_list_rel           mailing.contact.subscription
        # Not a real model until saas~13
        {gte_saas13} mail_message_res_partner_needaction_rel mail.notification
    """.format(
                action_report_model="ir.actions.report" if version_gte("10.saas~17") else "ir.actions.report.xml",
                gte_saas13="" if version_gte("9.saas~13") else "#",
            )
        )
    )
    return exceptions.get(table, table.replace("_", "."))


def env(cr):
    """
    Creates a new environment from cursor.
    ATTENTION: This function does NOT empty the cache maintained on the cursor
    for superuser with and empty environment. A call to invalidate_cache will
    most probably be necessary every time you directly modify something in database
    """
    try:
        from odoo.api import Environment
    except ImportError:
        try:
            from openerp.api import Environment
        except ImportError:
            v = release.major_version
            raise MigrationError("Hold on! There is not yet `Environment` in %s" % v)
    return Environment(cr, SUPERUSER_ID, {})


def remove_view(cr, xml_id=None, view_id=None, silent=False):
    """
    Recursively delete the given view and its inherited views, as long as they
    are part of a module. Will crash as soon as a custom view exists anywhere
    in the hierarchy.
    """
    assert bool(xml_id) ^ bool(view_id)
    if xml_id:
        view_id = ref(cr, xml_id)
        if not view_id:
            return

        module, _, name = xml_id.partition(".")
        cr.execute("SELECT model FROM ir_model_data WHERE module=%s AND name=%s", [module, name])

        [model] = cr.fetchone()
        if model != "ir.ui.view":
            raise ValueError("%r should point to a 'ir.ui.view', not a %r" % (xml_id, model))
    else:
        # search matching xmlid for logging or renaming of custom views
        cr.execute("SELECT module, name FROM ir_model_data WHERE model='ir.ui.view' AND res_id=%s", [view_id])
        if cr.rowcount:
            xml_id = "%s.%s" % cr.fetchone()
        else:
            xml_id = "?"

    cr.execute(
        """
        SELECT v.id, x.module || '.' || x.name, v.name
        FROM ir_ui_view v LEFT JOIN
           ir_model_data x ON (v.id = x.res_id AND x.model = 'ir.ui.view' AND x.module !~ '^_')
        WHERE v.inherit_id = %s;
    """,
        [view_id],
    )
    for child_id, child_xml_id, child_name in cr.fetchall():
        if child_xml_id:
            if not silent:
                _logger.info(
                    "remove deprecated built-in view %s (ID %s) as parent view %s (ID %s) is going to be removed",
                    child_xml_id,
                    child_id,
                    xml_id,
                    view_id,
                )
            remove_view(cr, child_xml_id, silent=True)
        else:
            if not silent:
                _logger.warning(
                    "deactivate deprecated custom view with ID %s as parent view %s (ID %s) is going to be removed",
                    child_id,
                    xml_id,
                    view_id,
                )
            disable_view_query = """
                UPDATE ir_ui_view
                SET name = (name || ' - old view, inherited from ' || %%s),
                    model = (model || '.disabled'),
                    inherit_id = NULL
                    %s
                    WHERE id = %%s
            """
            # In 8.0, disabling requires setting mode to 'primary'
            extra_set_sql = ""
            if column_exists(cr, "ir_ui_view", "mode"):
                extra_set_sql = ",  mode = 'primary' "

            disable_view_query = disable_view_query % extra_set_sql
            cr.execute(disable_view_query, (xml_id, child_id))
            add_to_migration_reports(
                {"id": child_id, "name": child_name},
                "Disabled views",
            )
    if not silent:
        _logger.info("remove deprecated built-in view %s (ID %s)", xml_id, view_id)
    remove_record(cr, ("ir.ui.view", view_id))


@contextmanager
def edit_view(cr, xmlid=None, view_id=None, skip_if_not_noupdate=True):
    """Contextmanager that may yield etree arch of a view.
    As it may not yield, you must use `skippable_cm`
        with util.skippable_cm(), util.edit_view(cr, 'xml.id') as arch:
            arch.attrib['string'] = 'My Form'
    """
    assert bool(xmlid) ^ bool(view_id), "You Must specify either xmlid or view_id"
    noupdate = True
    if xmlid:
        if "." not in xmlid:
            raise ValueError("Please use fully qualified name <module>.<name>")

        module, _, name = xmlid.partition(".")
        cr.execute(
            """
                SELECT res_id, noupdate
                  FROM ir_model_data
                 WHERE module = %s
                   AND name = %s
        """,
            [module, name],
        )
        data = cr.fetchone()
        if data:
            view_id, noupdate = data

    if view_id and not (skip_if_not_noupdate and not noupdate):
        arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
        cr.execute(
            """
                SELECT {arch}
                  FROM ir_ui_view
                 WHERE id=%s
            """.format(
                arch=arch_col
            ),
            [view_id],
        )
        [arch] = cr.fetchone() or [None]
        if arch:
            arch = lxml.etree.fromstring(arch)
            yield arch
            cr.execute(
                "UPDATE ir_ui_view SET {arch}=%s WHERE id=%s".format(arch=arch_col),
                [lxml.etree.tostring(arch, encoding="unicode"), view_id],
            )


def add_view(cr, name, model, view_type, arch_db, inherit_xml_id=None, priority=16):
    inherit_id = False
    if inherit_xml_id:
        inherit_id = ref(cr, inherit_xml_id)
        if not inherit_id:
            raise ValueError(
                "Unable to add view '%s' because its inherited view '%s' cannot be found!" % (name, inherit_xml_id)
            )
    arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
    cr.execute(
        """
        INSERT INTO ir_ui_view(name, "type",  model, inherit_id, mode, active, priority, %s)
        VALUES(%%(name)s, %%(view_type)s, %%(model)s, %%(inherit_id)s, %%(mode)s, 't', %%(priority)s, %%(arch_db)s)
    """
        % arch_col,
        {
            "name": name,
            "view_type": view_type,
            "model": model,
            "inherit_id": inherit_id,
            "mode": "extension" if inherit_id else "primary",
            "priority": priority,
            "arch_db": arch_db,
        },
    )


def remove_record(cr, name, deactivate=False, active_field="active"):
    if isinstance(name, basestring):
        if "." not in name:
            raise ValueError("Please use fully qualified name <module>.<name>")
        module, _, name = name.partition(".")
        cr.execute(
            """
                DELETE
                  FROM ir_model_data
                 WHERE module = %s
                   AND name = %s
             RETURNING model, res_id
        """,
            [module, name],
        )
        data = cr.fetchone()
        if not data:
            return
        model, res_id = data
        if model == "ir.ui.view":
            # NOTE: only done when a xmlid is given to avoid infinite recursion
            _logger.log(NEARLYWARN, "Removing view %r", name)
            return remove_view(cr, view_id=res_id)
    elif isinstance(name, tuple):
        if len(name) != 2:
            raise ValueError("Please use a 2-tuple (<model>, <res_id>)")
        model, res_id = name
    else:
        raise ValueError("Either use a fully qualified xmlid string <module>.<name> or a 2-tuple (<model>, <res_id>)")

    if model == "ir.ui.menu":
        _logger.log(NEARLYWARN, "Removing menu %r", name)
        return remove_menus(cr, [res_id])

    table = table_of_model(cr, model)
    try:
        with savepoint(cr):
            cr.execute('DELETE FROM "%s" WHERE id=%%s' % table, (res_id,))
    except Exception:
        if not deactivate or not active_field:
            raise
        cr.execute('UPDATE "%s" SET "%s"=%%s WHERE id=%%s' % (table, active_field), (False, res_id))
    else:
        for ir in indirect_references(cr, bound_only=True):
            query = 'DELETE FROM "{}" WHERE {} AND "{}"=%s'.format(ir.table, ir.model_filter(), ir.res_id)
            cr.execute(query, [model, res_id])
        _rm_refs(cr, model, [res_id])

    if model == "res.groups":
        # A group is gone, the auto-generated view `base.user_groups_view` is outdated.
        # Create a shim. It will be re-generated later by creating/updating groups or
        # explicitly in `base/0.0.0/end-user_groups_view.py`.
        arch_col = "arch_db" if column_exists(cr, "ir_ui_view", "arch_db") else "arch"
        cr.execute(
            "UPDATE ir_ui_view SET {} = '<form/>' WHERE id = %s".format(arch_col), [ref(cr, "base.user_groups_view")]
        )


def if_unchanged(cr, xmlid, callback, interval="1 minute"):
    assert "." in xmlid
    module, _, name = xmlid.partition(".")
    cr.execute("SELECT model, res_id FROM ir_model_data WHERE module=%s AND name=%s", [module, name])
    data = cr.fetchone()
    if not data:
        return
    model, res_id = data
    table = table_of_model(cr, model)
    cr.execute(
        """
        SELECT 1
          FROM {}
         WHERE id = %s
           -- Note: use a negative search to handle the case of NULL values in write/create_date
           AND write_date - create_date > interval %s
    """.format(
            table
        ),
        [res_id, interval],
    )
    if not cr.rowcount:
        callback(cr, xmlid)


def remove_menus(cr, menu_ids):
    if not menu_ids:
        return
    cr.execute(
        """
        WITH RECURSIVE tree(id) AS (
            SELECT id
              FROM ir_ui_menu
             WHERE id IN %s
             UNION
            SELECT m.id
              FROM ir_ui_menu m
              JOIN tree t ON (m.parent_id = t.id)
        )
        DELETE FROM ir_ui_menu m
              USING tree t
              WHERE m.id = t.id
          RETURNING m.id
    """,
        [tuple(menu_ids)],
    )
    ids = tuple(x[0] for x in cr.fetchall())
    if ids:
        cr.execute("DELETE FROM ir_model_data WHERE model='ir.ui.menu' AND res_id IN %s", [ids])


def rename_xmlid(cr, old, new, noupdate=None):
    if "." not in old or "." not in new:
        raise ValueError("Please use fully qualified name <module>.<name>")

    old_module, _, old_name = old.partition(".")
    new_module, _, new_name = new.partition(".")
    nu = "" if noupdate is None else (", noupdate=" + str(bool(noupdate)).lower())
    cr.execute(
        """UPDATE ir_model_data
                     SET module=%s, name=%s
                         {}
                   WHERE module=%s AND name=%s
               RETURNING model, res_id
               """.format(
            nu
        ),
        (new_module, new_name, old_module, old_name),
    )
    data = cr.fetchone()
    if data:
        model, rid = data
        if model == "ir.ui.view" and column_exists(cr, "ir_ui_view", "key"):
            cr.execute("UPDATE ir_ui_view SET key=%s WHERE id=%s AND key=%s", [new, rid, old])
        return rid
    return None


def ref(cr, xmlid):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            SELECT res_id
              FROM ir_model_data
             WHERE module = %s
               AND name = %s
    """,
        [module, name],
    )
    data = cr.fetchone()
    if data:
        return data[0]
    return None


def force_noupdate(cr, xmlid, noupdate=True, warn=False):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            UPDATE ir_model_data
               SET noupdate = %s
             WHERE module = %s
               AND name = %s
               AND noupdate != %s
    """,
        [noupdate, module, name, noupdate],
    )
    if noupdate is False and cr.rowcount and warn:
        _logger.warning("Customizations on `%s` might be lost!", xmlid)
    return cr.rowcount


def ensure_xmlid_match_record(cr, xmlid, model, values):
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")
    cr.execute(
        """
            SELECT id, res_id
              FROM ir_model_data
             WHERE module = %s
               AND name = %s
    """,
        [module, name],
    )

    table = table_of_model(cr, model)
    data = cr.fetchone()
    if data:
        data_id, res_id = data
        # check that record still exists
        cr.execute("SELECT id FROM %s WHERE id=%%s" % table, (res_id,))
        if cr.fetchone():
            return res_id
    else:
        data_id = None

    # search for existing record marching values
    where = []
    data = ()
    for k, v in values.items():
        if v:
            where += ["%s = %%s" % (k,)]
            data += (v,)
        else:
            where += ["%s IS NULL" % (k,)]
            data += ()

    query = ("SELECT id FROM %s WHERE " % table) + " AND ".join(where)
    cr.execute(query, data)
    record = cr.fetchone()
    if not record:
        return None

    res_id = record[0]

    if data_id:
        cr.execute(
            """
                UPDATE ir_model_data
                   SET res_id=%s
                 WHERE id=%s
        """,
            [res_id, data_id],
        )
    else:
        cr.execute(
            """
                INSERT INTO ir_model_data(module, name, model, res_id, noupdate)
                     VALUES (%s, %s, %s, %s, %s)
        """,
            [module, name, model, res_id, True],
        )

    return res_id


def update_record_from_xml(cr, xmlid, reset_write_metadata=True):
    # Force update of a record from xml file to bypass the noupdate flag
    if "." not in xmlid:
        raise ValueError("Please use fully qualified name <module>.<name>")

    module, _, name = xmlid.partition(".")

    cr.execute(
        """
        UPDATE ir_model_data d
           SET noupdate = false
          FROM ir_model_data o
         WHERE o.id = d.id
           AND d.module = %s
           AND d.name = %s
     RETURNING d.model, d.res_id, o.noupdate
    """,
        [module, name],
    )
    if not cr.rowcount:
        return
    model, res_id, noupdate = cr.fetchone()

    write_data = None
    table = table_of_model(cr, model)
    if reset_write_metadata:
        cr.execute("SELECT write_uid, write_date, id FROM {} WHERE id=%s".format(table), [res_id])
        write_data = cr.fetchone()

    xpath = "//*[@id='{module}.{name}' or @id='{name}']".format(module=module, name=name)
    # use a data tag inside openerp tag to be compatible with all supported versions
    new_root = lxml.etree.fromstring("<openerp><data/></openerp>")

    manifest = load_information_from_description_file(module)
    for f in manifest.get("data", []):
        if not f.endswith(".xml"):
            continue
        with file_open(os.path.join(module, f)) as fp:
            doc = lxml.etree.parse(fp)
            for node in doc.xpath(xpath):
                new_root[0].append(node)

    importer = xml_import(cr, module, idref={}, mode="update")
    kw = dict(mode="update") if parse_version("8.0") <= parse_version(release.series) <= parse_version("12.0") else {}
    importer.parse(new_root, **kw)

    if noupdate:
        force_noupdate(cr, xmlid, True)
    if reset_write_metadata and write_data:
        cr.execute("UPDATE {} SET write_uid=%s, write_date=%s WHERE id=%s".format(table), write_data)


def fix_wrong_m2o(cr, table, column, target, value=None):
    cr.execute(
        """
        WITH wrongs_m2o AS (
            SELECT s.id
              FROM {table} s
         LEFT JOIN {target} t
                ON s.{column} = t.id
             WHERE s.{column} IS NOT NULL
               AND t.id IS NULL
        )
        UPDATE {table} s
           SET {column}=%s
          FROM wrongs_m2o w
         WHERE s.id = w.id
    """.format(
            table=table, column=column, target=target
        ),
        [value],
    )


def ensure_m2o_func_field_data(cr, src_table, column, dst_table):
    """
    Fix broken m2o relations.
    If any `column` not present in `dst_table`, remove column from `src_table` in
    order to force recomputation of the function field
    WARN: only call this method on m2o function/related fields!!
    """
    if not column_exists(cr, src_table, column):
        return
    cr.execute(
        """
            SELECT count(1)
              FROM "{src_table}"
             WHERE "{column}" NOT IN (SELECT id FROM "{dst_table}")
        """.format(
            src_table=src_table, column=column, dst_table=dst_table
        )
    )
    if cr.fetchone()[0]:
        remove_column(cr, src_table, column, cascade=True)


def create_m2m(cr, m2m, fk1, fk2, col1=None, col2=None):
    if col1 is None:
        col1 = "%s_id" % fk1
    if col2 is None:
        col2 = "%s_id" % fk2

    cr.execute(
        """
        CREATE TABLE {m2m}(
            {col1} integer NOT NULL REFERENCES {fk1}(id) ON DELETE CASCADE,
            {col2} integer NOT NULL REFERENCES {fk2}(id) ON DELETE CASCADE,
            PRIMARY KEY ({col1}, {col2})
        );
        CREATE INDEX ON {m2m}({col2}, {col1});
    """.format(
            **locals()
        )
    )


def fixup_m2m(cr, m2m, fk1, fk2, col1=None, col2=None):
    if col1 is None:
        col1 = "%s_id" % fk1
    if col2 is None:
        col2 = "%s_id" % fk2

    if not table_exists(cr, m2m):
        return

    # cleanup
    cr.execute(
        """
        DELETE FROM {m2m} t
              WHERE {col1} IS NULL
                 OR {col2} IS NULL
                 OR NOT EXISTS (SELECT id FROM {fk1} WHERE id=t.{col1})
                 OR NOT EXISTS (SELECT id FROM {fk2} WHERE id=t.{col2})
    """.format(
            **locals()
        )
    )
    deleted = cr.rowcount
    if deleted:
        _logger.debug("%(m2m)s: removed %(deleted)d invalid rows", locals())

    # remove duplicated rows
    cr.execute(
        """
        DELETE FROM {m2m}
              WHERE ctid IN (SELECT ctid
                               FROM (SELECT ctid,
                                            ROW_NUMBER() OVER (PARTITION BY {col1}, {col2}
                                                                   ORDER BY ctid) as rnum
                                       FROM {m2m}) t
                              WHERE t.rnum > 1)
    """.format(
            **locals()
        )
    )
    deleted = cr.rowcount
    if deleted:
        _logger.debug("%(m2m)s: removed %(deleted)d duplicated rows", locals())

    # set not null
    cr.execute("ALTER TABLE {m2m} ALTER COLUMN {col1} SET NOT NULL".format(**locals()))
    cr.execute("ALTER TABLE {m2m} ALTER COLUMN {col2} SET NOT NULL".format(**locals()))

    # create  missing or bad fk
    target = target_of(cr, m2m, col1)
    if target and target[:2] != (fk1, "id"):
        cr.execute("ALTER TABLE {m2m} DROP CONSTRAINT {con}".format(m2m=m2m, con=target[2]))
        target = None
    if not target:
        _logger.debug("%(m2m)s: add FK %(col1)s -> %(fk1)s", locals())
        cr.execute("ALTER TABLE {m2m} ADD FOREIGN KEY ({col1}) REFERENCES {fk1} ON DELETE CASCADE".format(**locals()))

    target = target_of(cr, m2m, col2)
    if target and target[:2] != (fk2, "id"):
        cr.execute("ALTER TABLE {m2m} DROP CONSTRAINT {con}".format(m2m=m2m, con=target[2]))
        target = None
    if not target:
        _logger.debug("%(m2m)s: add FK %(col2)s -> %(fk2)s", locals())
        cr.execute("ALTER TABLE {m2m} ADD FOREIGN KEY ({col2}) REFERENCES {fk2} ON DELETE CASCADE".format(**locals()))

    # create indexes
    idx1 = get_index_on(cr, m2m, col1, col2)
    idx2 = get_index_on(cr, m2m, col2, col1)

    if not idx1 and not idx2:
        # No index at all
        cr.execute('ALTER TABLE "%s" ADD PRIMARY KEY("%s", "%s")' % (m2m, col1, col2))
        cr.execute('CREATE INDEX ON "%s" ("%s", "%s")' % (m2m, col2, col1))
    elif idx1 and idx2:
        if not idx1[1] and not idx2[1]:
            # if both are not unique, create a PK
            cr.execute('ALTER TABLE "%s" ADD PRIMARY KEY("%s", "%s")' % (m2m, col1, col2))
    else:
        # only 1 index exist, create the second one
        # determine which one is missing
        fmt = (m2m, col2, col1) if idx1 else (m2m, col1, col2)
        if (idx1 or idx2)[1]:
            # the existing index is unique, create a normal index
            cr.execute('CREATE INDEX ON "%s" ("%s", "%s")' % fmt)
        else:
            # create a PK (unqiue index)
            cr.execute('ALTER TABLE "%s" ADD PRIMARY KEY("%s", "%s")' % fmt)

    # remove indexes on 1 column only
    idx = get_index_on(cr, m2m, col1)
    if idx:
        cr.execute("DROP INDEX %s" % idx[0])
    idx = get_index_on(cr, m2m, col2)
    if idx:
        cr.execute("DROP INDEX %s" % idx[0])


def uniq_tags(cr, model, uniq_column="name", order="id"):
    """
    Deduplicated "tag" models entries.
    Should only be referenced as many2many
    By using `uniq_column=lower(name)` and `order=name`
    you can prioritize tags in CamelCase/UPPERCASE.
    """
    table = table_of_model(cr, model)
    upds = []
    for ft, fc, _, da in get_fk(cr, table):
        assert da == "c"  # should be a ondelete=cascade fk
        cols = get_columns(cr, ft, ignore=(fc,))[0]
        assert len(cols) == 1  # it's a m2, should have only 2 columns

        upds.append(
            """
            INSERT INTO {rel}({c1}, {c2})
                 SELECT r.{c1}, d.id
                   FROM {rel} r
                   JOIN dups d ON (r.{c2} = ANY(d.others))
                 EXCEPT
                 SELECT r.{c1}, r.{c2}
                   FROM {rel} r
                   JOIN dups d ON (r.{c2} = d.id)
        """.format(
                rel=ft, c1=cols[0], c2=fc
            )
        )

    assert upds  # if not m2m found, there is something wrong...

    updates = ",".join("_upd_%s AS (%s)" % x for x in enumerate(upds))
    query = """
        WITH dups AS (
            SELECT (array_agg(id order by {order}))[1] as id,
                   (array_agg(id order by {order}))[2:array_length(array_agg(id), 1)] as others
              FROM {table}
          GROUP BY {uniq_column}
            HAVING count(id) > 1
        ),
        _upd_imd AS (
            UPDATE ir_model_data x
               SET res_id = d.id
              FROM dups d
             WHERE x.model = %s
               AND x.res_id = ANY(d.others)
        ),
        {updates}
        DELETE FROM {table} WHERE id IN (SELECT unnest(others) FROM dups)
    """.format(
        **locals()
    )

    cr.execute(query, [model])


def delete_unused(cr, *xmlids):
    select_xids = " UNION ".join(
        [
            cr.mogrify("SELECT %s::varchar as module, %s::varchar as name", [module, name]).decode()
            for xmlid in xmlids
            for module, _, name in [xmlid.partition(".")]
        ]
    )

    cr.execute(
        """
       WITH xids AS (
         {}
       ),
       _upd AS (
            UPDATE ir_model_data d
               SET noupdate = true
              FROM xids x
             WHERE d.module = x.module
               AND d.name = x.name
         RETURNING d.model, d.res_id
       )
       SELECT model, array_agg(res_id)
         FROM _upd
     GROUP BY model
    """.format(
            select_xids
        )
    )

    for model, ids in cr.fetchall():
        table = table_of_model(cr, model)

        sub = " UNION ".join(
            [
                'SELECT 1 FROM "{}" x WHERE x."{}" = t.id'.format(fk_tbl, fk_col)
                for fk_tbl, fk_col, _, fk_act in get_fk(cr, table, quote_ident=False)
                # ignore "on delete cascade" fk (they are indirect dependencies (lines or m2m))
                if fk_act != "c"
                # ignore children records unless the deletion is restricted
                if not (fk_tbl == table and fk_act != "r")
            ]
        )
        if sub:
            cr.execute(
                """
                SELECT id
                  FROM "{}" t
                 WHERE id = ANY(%s)
                   AND NOT EXISTS({})
            """.format(
                    table, sub
                ),
                [list(ids)],
            )
            ids = map(itemgetter(0), cr.fetchall())

        for tid in ids:
            remove_record(cr, (model, tid))


def modules_installed(cr, *modules):
    """return True if all `modules` are (about to be) installed"""
    assert modules
    cr.execute(
        """
            SELECT count(1)
              FROM ir_module_module
             WHERE name IN %s
               AND state IN %s
    """,
        [modules, _INSTALLED_MODULE_STATES],
    )
    return cr.fetchone()[0] == len(modules)


def module_installed(cr, module):
    return modules_installed(cr, module)


def uninstall_module(cr, module):

    cr.execute("SELECT id FROM ir_module_module WHERE name=%s", (module,))
    (mod_id,) = cr.fetchone() or [None]
    if not mod_id:
        return

    # delete constraints only owned by this module
    cr.execute(
        """
            SELECT name
              FROM ir_model_constraint
          GROUP BY name
            HAVING array_agg(module) = %s
    """,
        ([mod_id],),
    )

    constraints = tuple(map(itemgetter(0), cr.fetchall()))
    if constraints:
        cr.execute(
            """
                SELECT table_name, constraint_name
                  FROM information_schema.table_constraints
                 WHERE constraint_name IN %s
        """,
            [constraints],
        )
        for table, constraint in cr.fetchall():
            cr.execute('ALTER TABLE "%s" DROP CONSTRAINT "%s"' % (table, constraint))

    cr.execute(
        """
            DELETE
              FROM ir_model_constraint
             WHERE module = %s
    """,
        [mod_id],
    )

    # delete data
    model_ids, field_ids, menu_ids = [], [], []
    cr.execute(
        """
            SELECT model, res_id
              FROM ir_model_data d
             WHERE NOT EXISTS (SELECT 1
                                 FROM ir_model_data
                                WHERE id != d.id
                                  AND res_id = d.res_id
                                  AND model = d.model
                                  AND module != d.module)
               AND module = %s
               AND model != 'ir.module.module'
          ORDER BY id DESC
    """,
        [module],
    )
    for model, res_id in cr.fetchall():
        if model == "ir.model":
            model_ids.append(res_id)
        elif model == "ir.model.fields":
            field_ids.append(res_id)
        elif model == "ir.ui.menu":
            menu_ids.append(res_id)
        elif model == "ir.ui.view":
            remove_view(cr, view_id=res_id, silent=True)
        else:
            remove_record(cr, (model, res_id))

    if menu_ids:
        remove_menus(cr, menu_ids)

    # remove relations
    cr.execute(
        """
            SELECT name
              FROM ir_model_relation
          GROUP BY name
            HAVING array_agg(module) = %s
    """,
        ([mod_id],),
    )
    relations = tuple(map(itemgetter(0), cr.fetchall()))
    cr.execute("DELETE FROM ir_model_relation WHERE module=%s", (mod_id,))
    if relations:
        cr.execute("SELECT table_name FROM information_schema.tables WHERE table_name IN %s", (relations,))
        for (rel,) in cr.fetchall():
            cr.execute('DROP TABLE "%s" CASCADE' % (rel,))

    if model_ids:
        cr.execute("SELECT model FROM ir_model WHERE id IN %s", [tuple(model_ids)])
        for (model,) in cr.fetchall():
            delete_model(cr, model)

    if field_ids:
        cr.execute("SELECT model, name FROM ir_model_fields WHERE id IN %s", [tuple(field_ids)])
        for model, name in cr.fetchall():
            remove_field(cr, model, name)

    cr.execute("DELETE FROM ir_model_data WHERE model='ir.module.module' AND res_id=%s", [mod_id])
    cr.execute("DELETE FROM ir_model_data WHERE module=%s", (module,))
    cr.execute("DELETE FROM ir_translation WHERE module=%s", [module])
    cr.execute("UPDATE ir_module_module SET state='uninstalled' WHERE name=%s", (module,))


def remove_module(cr, module):
    """Uninstall the module and delete references to it
    Ensure to reassign records before calling this method
    """
    # NOTE: we cannot use the uninstall of module because the given
    # module need to be currently installed and running as deletions
    # are made using orm.

    uninstall_module(cr, module)
    cr.execute("DELETE FROM ir_module_module WHERE name=%s", (module,))
    cr.execute("DELETE FROM ir_module_module_dependency WHERE name=%s", (module,))


def _update_view_key(cr, old, new):
    # update view key for renamed & merged modules
    if not column_exists(cr, "ir_ui_view", "key"):
        return
    cr.execute(
        """
        UPDATE ir_ui_view v
           SET key = CONCAT(%s, '.', x.name)
          FROM ir_model_data x
         WHERE x.model = 'ir.ui.view'
           AND x.res_id = v.id
           AND x.module = %s
           AND v.key = CONCAT(x.module, '.', x.name)
    """,
        [new, old],
    )


def rename_module(cr, old, new):
    cr.execute("UPDATE ir_module_module SET name=%s WHERE name=%s", (new, old))
    cr.execute("UPDATE ir_module_module_dependency SET name=%s WHERE name=%s", (new, old))
    _update_view_key(cr, old, new)
    cr.execute("UPDATE ir_model_data SET module=%s WHERE module=%s", (new, old))
    cr.execute("UPDATE ir_translation SET module=%s WHERE module=%s", [new, old])

    mod_old = "module_" + old
    mod_new = "module_" + new
    cr.execute(
        """
            UPDATE ir_model_data
               SET name = %s
             WHERE name = %s
               AND module = %s
               AND model = %s
    """,
        [mod_new, mod_old, "base", "ir.module.module"],
    )


def merge_module(cr, old, into, without_deps=False):
    """Move all references of module `old` into module `into`"""
    cr.execute("SELECT name, id FROM ir_module_module WHERE name IN %s", [(old, into)])
    mod_ids = dict(cr.fetchall())

    if old not in mod_ids:
        # this can happen in case of temp modules added after a release if the database does not
        # know about this module, i.e: account_full_reconcile in 9.0
        # `into` should be know. Let it crash if not
        _logger.log(NEARLYWARN, "Unknow module %s. Skip merge into %s.", old, into)
        return

    def _up(table, old, new):
        cr.execute(
            """
                UPDATE ir_model_{0} x
                   SET module=%s
                 WHERE module=%s
                   AND NOT EXISTS(SELECT 1
                                    FROM ir_model_{0} y
                                   WHERE y.name = x.name
                                     AND y.module = %s)
        """.format(
                table
            ),
            [new, old, new],
        )

        if table == "data":
            cr.execute(
                """
                SELECT model, array_agg(res_id)
                  FROM ir_model_data
                 WHERE module=%s
                   AND model NOT LIKE 'ir.model%%'
                   AND model NOT LIKE 'ir.module.module%%'
              GROUP BY model
            """,
                [old],
            )
            for model, res_ids in cr.fetchall():
                if model == "ir.ui.view":
                    for v in res_ids:
                        remove_view(cr, view_id=v, silent=True)
                elif model == "ir.ui.menu":
                    remove_menus(cr, tuple(res_ids))
                else:
                    for r in res_ids:
                        remove_record(cr, (model, r))

        cr.execute("DELETE FROM ir_model_{} WHERE module=%s".format(table), [old])

    _up("constraint", mod_ids[old], mod_ids[into])
    _up("relation", mod_ids[old], mod_ids[into])
    _update_view_key(cr, old, into)
    _up("data", old, into)
    cr.execute("UPDATE ir_translation SET module=%s WHERE module=%s", [into, old])

    # update dependencies
    if not without_deps:
        cr.execute(
            """
            INSERT INTO ir_module_module_dependency(module_id, name)
            SELECT module_id, %s
              FROM ir_module_module_dependency d
             WHERE name=%s
               AND NOT EXISTS(SELECT 1
                                FROM ir_module_module_dependency o
                               WHERE o.module_id = d.module_id
                                 AND o.name=%s)
        """,
            [into, old, into],
        )

    cr.execute("DELETE FROM ir_module_module WHERE name=%s RETURNING state", [old])
    [state] = cr.fetchone()
    cr.execute("DELETE FROM ir_module_module_dependency WHERE name=%s", [old])
    cr.execute("DELETE FROM ir_model_data WHERE model='ir.module.module' AND res_id=%s", [mod_ids[old]])
    if state in _INSTALLED_MODULE_STATES:
        force_install_module(cr, into)


def force_install_module(cr, module, if_installed=None):
    subquery = ""
    subparams = ()
    if if_installed:
        subquery = """AND EXISTS(SELECT 1 FROM ir_module_module
                                  WHERE name IN %s
                                    AND state IN %s)"""
        subparams = (tuple(if_installed), _INSTALLED_MODULE_STATES)

    cr.execute(
        """
        WITH RECURSIVE deps (mod_id, dep_name) AS (
              SELECT m.id, d.name from ir_module_module_dependency d
              JOIN ir_module_module m on (d.module_id = m.id)
              WHERE m.name = %s
            UNION
              SELECT m.id, d.name from ir_module_module m
              JOIN deps ON deps.dep_name = m.name
              JOIN ir_module_module_dependency d on (d.module_id = m.id)
        )
        UPDATE ir_module_module m
           SET state = CASE WHEN state = 'to remove' THEN 'to upgrade'
                            WHEN state = 'uninstalled' THEN 'to install'
                            ELSE state
                       END,
               demo=(select demo from ir_module_module where name='base')
          FROM deps d
         WHERE m.id = d.mod_id
           {}
     RETURNING m.name, m.state
    """.format(
            subquery
        ),
        (module,) + subparams,
    )

    states = dict(cr.fetchall())
    # auto_install modules...
    toinstall = [m for m in states if states[m] == "to install"]
    if toinstall:
        # Same algo as ir.module.module.button_install(): https://git.io/fhCKd
        dep_match = ""
        if column_exists(cr, "ir_module_module_dependency", "auto_install_required"):
            dep_match = "AND d.auto_install_required = TRUE AND e.auto_install_required = TRUE"

        cr.execute(
            """
            SELECT on_me.name
              FROM ir_module_module_dependency d
              JOIN ir_module_module on_me ON on_me.id = d.module_id
              JOIN ir_module_module_dependency e ON e.module_id = on_me.id
              JOIN ir_module_module its_deps ON its_deps.name = e.name
             WHERE d.name = ANY(%s)
               AND on_me.state = 'uninstalled'
               AND on_me.auto_install = TRUE
               {}
          GROUP BY on_me.name
            HAVING
                   -- are all dependencies (to be) installed?
                   array_agg(its_deps.state)::text[] <@ %s
        """.format(
                dep_match
            ),
            [toinstall, list(_INSTALLED_MODULE_STATES)],
        )
        for (mod,) in cr.fetchall():
            _logger.debug("auto install module %r due to module %r being force installed", mod, module)
            force_install_module(cr, mod)

    # TODO handle module exclusions

    return states.get(module)


def _assert_modules_exists(cr, *modules):
    assert modules
    cr.execute("SELECT name FROM ir_module_module WHERE name IN %s", [modules])
    existing_modules = {m[0] for m in cr.fetchall()}
    unexisting_modules = set(modules) - existing_modules
    if unexisting_modules:
        raise AssertionError("Unexisting modules: {}".format(", ".join(unexisting_modules)))


def new_module_dep(cr, module, new_dep):
    assert isinstance(new_dep, basestring)
    _assert_modules_exists(cr, module, new_dep)
    # One new dep at a time
    cr.execute(
        """
            INSERT INTO ir_module_module_dependency(name, module_id)
                       SELECT %s, id
                         FROM ir_module_module m
                        WHERE name=%s
                          AND NOT EXISTS(SELECT 1
                                           FROM ir_module_module_dependency
                                          WHERE module_id = m.id
                                            AND name=%s)
    """,
        [new_dep, module, new_dep],
    )

    # Update new_dep state depending on module state
    cr.execute("SELECT state FROM ir_module_module WHERE name = %s", [module])
    mod_state = (cr.fetchone() or ["n/a"])[0]
    if mod_state in _INSTALLED_MODULE_STATES:
        # Module was installed, need to install all its deps, recursively,
        # to make sure the new dep is installed
        force_install_module(cr, module)


def remove_module_deps(cr, module, old_deps):
    assert isinstance(old_deps, (collections.Sequence, collections.Set)) and not isinstance(old_deps, basestring)
    # As the goal is to have dependencies removed, the objective is reached even when they don't exist.
    # Therefore, we don't need to assert their existence (at the cost of missing typos).
    cr.execute(
        """
            DELETE
              FROM ir_module_module_dependency
             WHERE module_id = (SELECT id
                                  FROM ir_module_module
                                 WHERE name = %s)
               AND name IN %s
    """,
        [module, tuple(old_deps)],
    )


def module_deps_diff(cr, module, plus=(), minus=()):
    for new_dep in plus:
        new_module_dep(cr, module, new_dep)
    if minus:
        remove_module_deps(cr, module, tuple(minus))


def module_auto_install(cr, module, auto_install):
    if column_exists(cr, "ir_module_module_dependency", "auto_install_required"):
        params = []
        if auto_install is True:
            value = "TRUE"
        elif auto_install:
            value = "(name = ANY(%s))"
            params = [list(auto_install)]
        else:
            value = "FALSE"

        cr.execute(
            """
            UPDATE ir_module_module_dependency
               SET auto_install_required = {}
             WHERE module_id = (SELECT id
                                  FROM ir_module_module
                                 WHERE name = %s)
        """.format(
                value
            ),
            params + [module],
        )

    cr.execute("UPDATE ir_module_module SET auto_install = %s WHERE name = %s", [auto_install is not False, module])


def new_module(cr, module, deps=(), auto_install=False):
    if deps:
        _assert_modules_exists(cr, *deps)

    cr.execute("SELECT count(1) FROM ir_module_module WHERE name = %s", [module])
    if cr.fetchone()[0]:
        # Avoid duplicate entries for module which is already installed,
        # even before it has become standard module in new version
        # Also happen for modules added afterward, which should be added by multiple series.
        return

    if deps and auto_install:
        to_check = deps if auto_install is True else auto_install
        state = "to install" if modules_installed(cr, *to_check) else "uninstalled"
    else:
        state = "uninstalled"
    cr.execute(
        """
        INSERT INTO ir_module_module (name, state, demo)
             VALUES (%s, %s, (SELECT demo FROM ir_module_module WHERE name='base'))
          RETURNING id
    """,
        [module, state],
    )
    (new_id,) = cr.fetchone()

    cr.execute(
        """
        INSERT INTO ir_model_data (name, module, noupdate, model, res_id)
             VALUES ('module_'||%s, 'base', 't', 'ir.module.module', %s)
    """,
        [module, new_id],
    )

    for dep in deps:
        new_module_dep(cr, module, dep)

    module_auto_install(cr, module, auto_install)


def force_migration_of_fresh_module(cr, module, init=True):
    """It may appear that new (or forced installed) modules need a migration script to grab data
    form other module. (we cannot add a pre-init hook on the fly)
    Being in init mode may make sens in some situations (when?) but has the nasty side effect
    of not respecting noupdate flags (in xml file nor in ir_model_data) which can be quite
    problematic
    """
    filename, _ = frame_codeinfo(currentframe(), 1)
    version = ".".join(filename.split(os.path.sep)[-2].split(".")[:2])

    # Force module state to be in `to upgrade`.
    # Needed for migration script execution. See http://git.io/vnF7f
    cr.execute(
        """
            UPDATE ir_module_module
               SET state='to upgrade',
                   latest_version=%s
             WHERE name=%s
               AND state='to install'
         RETURNING id
    """,
        [version, module],
    )
    if init and cr.rowcount:
        # Force module in `init` mode beside its state is forced to `to upgrade`
        # See http://git.io/vnF7O
        odoo.tools.config["init"][module] = "oh yeah!"


def column_exists(cr, table, column):
    return column_type(cr, table, column) is not None


def column_type(cr, table, column):
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    cr.execute(
        """
            SELECT udt_name
              FROM information_schema.columns
             WHERE table_name = %s
               AND column_name = %s
    """,
        [table, column],
    )

    r = cr.fetchone()
    return r[0] if r else None


def create_column(cr, table, column, definition):
    aliases = {
        "boolean": "bool",
        "smallint": "int2",
        "integer": "int4",
        "bigint": "int8",
        "real": "float4",
        "double precision": "float8",
        "character varying": "varchar",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
    }
    definition = aliases.get(definition.lower(), definition)

    curtype = column_type(cr, table, column)
    if curtype:
        if curtype != definition:
            _logger.error("%s.%s already exists but is %r instead of %r", table, column, curtype, definition)
        return False
    else:
        cr.execute("""ALTER TABLE "%s" ADD COLUMN "%s" %s""" % (table, column, definition))
        return True


def remove_column(cr, table, column, cascade=False):
    if column_exists(cr, table, column):
        drop_depending_views(cr, table, column)
        drop_cascade = " CASCADE" if cascade else ""
        cr.execute('ALTER TABLE "{}" DROP COLUMN "{}"{}'.format(table, column, drop_cascade))


def table_exists(cr, table):
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    cr.execute(
        """
            SELECT 1
              FROM information_schema.tables
             WHERE table_name = %s
               AND table_type = 'BASE TABLE'
    """,
        [table],
    )
    return cr.fetchone() is not None


def view_exists(cr, view):
    if "." in view:
        raise SleepyDeveloperError("view name cannot contains dot")
    cr.execute("SELECT 1 FROM information_schema.views WHERE table_name=%s", [view])
    return bool(cr.rowcount)


def get_fk(cr, table, quote_ident=True):
    """return the list of foreign keys pointing to `table`
    returns a 4 tuple: (foreign_table, foreign_column, constraint_name, on_delete_action)
    Foreign key deletion action code:
        a = no action, r = restrict, c = cascade, n = set null, d = set default
    """
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    funk = "quote_ident" if quote_ident else "concat"
    q = """SELECT {funk}(cl1.relname) as table,
                  {funk}(att1.attname) as column,
                  {funk}(con.conname) as conname,
                  con.confdeltype
             FROM pg_constraint as con, pg_class as cl1, pg_class as cl2,
                  pg_attribute as att1, pg_attribute as att2
            WHERE con.conrelid = cl1.oid
              AND con.confrelid = cl2.oid
              AND array_lower(con.conkey, 1) = 1
              AND con.conkey[1] = att1.attnum
              AND att1.attrelid = cl1.oid
              AND cl2.relname = %s
              AND att2.attname = 'id'
              AND array_lower(con.confkey, 1) = 1
              AND con.confkey[1] = att2.attnum
              AND att2.attrelid = cl2.oid
              AND con.contype = 'f'
    """.format(
        funk=funk
    )
    cr.execute(q, (table,))
    return cr.fetchall()


def target_of(cr, table, column):
    """
    Return the target of a foreign key.
    Returns None if there is not foreign key on given column.
    returns a 3-tuple (foreign_table, foreign_column, constraint_name)
    """
    cr.execute(
        """
        SELECT quote_ident(cl2.relname) as table,
               quote_ident(att2.attname) as column,
               quote_ident(con.conname) as conname
        FROM pg_constraint con
        JOIN pg_class cl1 ON (con.conrelid = cl1.oid)
        JOIN pg_attribute att1 ON (    array_lower(con.conkey, 1) = 1
                                    AND con.conkey[1] = att1.attnum
                                    AND att1.attrelid = cl1.oid)
        JOIN pg_class cl2 ON (con.confrelid = cl2.oid)
        JOIN pg_attribute att2 ON (    array_lower(con.confkey, 1) = 1
                                    AND con.confkey[1] = att2.attnum
                                    AND att2.attrelid = cl2.oid)
        WHERE cl1.relname = %s
        AND att1.attname = %s
        AND con.contype = 'f'
    """,
        [table, column],
    )
    return cr.fetchone()


def get_index_on(cr, table, *columns):
    """
    return an optional tuple (index_name, unique, pk)
    NOTE: column order is respected
    """
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    if cr._cnx.server_version >= 90500:
        position = "array_position(x.indkey, x.unnest_indkey)"
    else:
        # array_position does not exists prior postgresql 9.5
        position = "strpos(array_to_string(x.indkey::int4[] || 0, ','), x.unnest_indkey::varchar || ',')"
    cr.execute(
        """
        SELECT name, indisunique, indisprimary
          FROM (SELECT quote_ident(i.relname) as name,
                       x.indisunique, x.indisprimary,
                       array_agg(a.attname::text order by {}) as attrs
                  FROM (select *, unnest(indkey) as unnest_indkey from pg_index) x
                  JOIN pg_class c ON c.oid = x.indrelid
                  JOIN pg_class i ON i.oid = x.indexrelid
                  JOIN pg_attribute a ON (a.attrelid=c.oid AND a.attnum=x.unnest_indkey)
                 WHERE (c.relkind = ANY (ARRAY['r'::"char", 'm'::"char"]))
                   AND i.relkind = 'i'::"char"
                   AND c.relname = %s
              GROUP BY 1, 2, 3
          ) idx
         WHERE attrs = %s
    """.format(
            position
        ),
        [table, list(columns)],
    )
    return cr.fetchone()


@contextmanager
def disabled_index_on(cr, table_name):
    """
    This method will disable indexes on one table, perform your operation, then re-enable indices
    and reindex the table. Usefull for mass updates.
    Usage:
    with disabled_index_on(cr, 'my_big_table'):
        my_big_operation()
    """
    if "." in table_name:
        raise SleepyDeveloperError("table name cannot contains dot")
    cr.execute(
        """
        UPDATE pg_index
        SET indisready=false
        WHERE indrelid = (
            SELECT oid
            FROM pg_class
            WHERE relname=%s
        )
    """,
        [table_name],
    )
    yield
    cr.execute(
        """
        UPDATE pg_index
        SET indisready=true
        WHERE indrelid = (
            SELECT oid
            FROM pg_class
            WHERE relname=%s
        )
    """,
        [table_name],
    )
    cr.execute('REINDEX TABLE "%s"' % table_name)


def create_index(cr, name, table_name, *columns):
    # create index if table and columns exists and index don't already exists
    if "." in table_name:
        raise SleepyDeveloperError("table name cannot contains dot")
    if (
        columns
        and all(column_exists(cr, table_name, c) for c in columns)
        and get_index_on(cr, table_name, *columns) is None
    ):
        cr.execute(
            "CREATE INDEX {index_name} ON {table_name}({columns})".format(
                index_name=name, table_name=table_name, columns=",".join(columns)
            )
        )
        return True
    return False


@contextmanager
def temp_index(cr, table, *columns):
    # create a temporary index that will be removed at the end of the contextmanager
    assert columns
    name = "_".join(("_upg", table) + columns + (hex(int(time.time() * 1000))[2:],))
    create_index(cr, name, table, *columns)
    try:
        yield
    finally:
        cr.execute('DROP INDEX IF EXISTS "{}"'.format(name))


def get_depending_views(cr, table, column):
    # http://stackoverflow.com/a/11773226/75349
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    q = """
        SELECT distinct quote_ident(dependee.relname)
        FROM pg_depend
        JOIN pg_rewrite ON pg_depend.objid = pg_rewrite.oid
        JOIN pg_class as dependee ON pg_rewrite.ev_class = dependee.oid
        JOIN pg_class as dependent ON pg_depend.refobjid = dependent.oid
        JOIN pg_attribute ON pg_depend.refobjid = pg_attribute.attrelid
            AND pg_depend.refobjsubid = pg_attribute.attnum
        WHERE dependent.relname = %s
        AND pg_attribute.attnum > 0
        AND pg_attribute.attname = %s
        AND dependee.relkind='v'
    """
    cr.execute(q, [table, column])
    return map(itemgetter(0), cr.fetchall())


def get_columns(cr, table, ignore=("id",), extra_prefixes=None):
    """return the list of columns in table (minus ignored ones)
    can also returns the list multiple times with different prefixes.
    This can be used to duplicating records (INSERT SELECT from the same table)
    """
    if "." in table:
        raise SleepyDeveloperError("table name cannot contains dot")
    select = "quote_ident(column_name)"
    params = []
    if extra_prefixes:
        select = ",".join([select] + ["concat(%%s, '.', %s)" % select] * len(extra_prefixes))
        params = list(extra_prefixes)

    cr.execute(
        """
            SELECT {select}
              FROM information_schema.columns
             WHERE table_name=%s
               AND column_name NOT IN %s
    """.format(
            select=select
        ),
        params + [table, ignore],
    )
    return list(zip(*cr.fetchall()))


def find_new_table_column_name(cr, table, name):
    columns = get_columns(cr, table)
    i = 0
    while name in columns:
        i += 1
        name = name + "_" + i
    return name


def drop_depending_views(cr, table, column):
    """drop views depending on a field to allow the ORM to resize it in-place"""
    for v in get_depending_views(cr, table, column):
        cr.execute("DROP VIEW IF EXISTS {} CASCADE".format(v))


def remove_field(cr, model, fieldname, cascade=False, drop_column=True):
    if fieldname == "id":
        # called by `remove_module`. May happen when a model defined in a removed module was
        # overwritten by another module in previous version.
        return remove_model(cr, model)

    ENVIRON["__renamed_fields"][model].add(fieldname)

    # clean dashboards' `group_by`
    cr.execute(
        """
            SELECT array_agg(f.name), array_agg(aw.id)
              FROM ir_model_fields f
              JOIN ir_act_window aw
                ON aw.res_model = f.model
             WHERE f.model = %s
               AND f.name = %s
          GROUP BY f.model
        """,
        [model, fieldname],
    )
    for fields, actions in cr.fetchall():
        cr.execute(
            """
            SELECT id, arch
              FROM ir_ui_view_custom
             WHERE arch ~ %s
        """,
            ["name=[\"'](%s)[\"']" % "|".join(map(str, actions))],
        )
        for id, arch in ((x, lxml.etree.fromstring(y)) for x, y in cr.fetchall()):
            for action in arch.iterfind(".//action"):
                context = eval(action.get("context", "{}"), UnquoteEvalContext())
                if context.get("group_by"):
                    context["group_by"] = list(set(context["group_by"]) - set(fields))
                    action.set("context", unicode(context))
            cr.execute(
                """
                    UPDATE ir_ui_view_custom
                       SET arch = %s
                     WHERE id = %s
                """,
                [lxml.etree.tostring(arch, encoding="unicode"), id],
            )

    cr.execute(
        """
        DELETE FROM ir_server_object_lines
              WHERE col1 IN (SELECT id
                               FROM ir_model_fields
                              WHERE model = %s
                                AND name = %s)
    """,
        [model, fieldname],
    )
    cr.execute("DELETE FROM ir_model_fields WHERE model=%s AND name=%s RETURNING id", (model, fieldname))
    fids = tuple(map(itemgetter(0), cr.fetchall()))
    if fids:
        cr.execute("DELETE FROM ir_model_data WHERE model=%s AND res_id IN %s", ("ir.model.fields", fids))

    # cleanup translations
    cr.execute(
        """
       DELETE FROM ir_translation
        WHERE name=%s
          AND type in ('field', 'help', 'model', 'model_terms', 'selection')   -- ignore wizard_* translations
    """,
        ["%s,%s" % (model, fieldname)],
    )

    # remove default values set for aliases
    if column_exists(cr, "mail_alias", "alias_defaults"):
        cr.execute(
            """
            SELECT a.id, a.alias_defaults
              FROM mail_alias a
              JOIN ir_model m ON m.id = a.alias_model_id
             WHERE m.model = %s
               AND a.alias_defaults ~ %s
        """,
            [model, r"\y%s\y" % (fieldname,)],
        )
        for alias_id, defaults in cr.fetchall():
            try:
                defaults = dict(safe_eval(defaults))  # XXX literal_eval should works.
            except Exception:
                continue
            defaults.pop(fieldname, None)
            cr.execute("UPDATE mail_alias SET alias_defaults = %s WHERE id = %s", [repr(defaults), alias_id])

    # if field was a binary field stored as attachment, clean them...
    if column_exists(cr, "ir_attachment", "res_field"):
        parallel_execute(
            cr,
            explode_query(
                cr,
                cr.mogrify(
                    "DELETE FROM ir_attachment WHERE res_model = %s AND res_field = %s", [model, fieldname]
                ).decode(),
            ),
        )

    table = table_of_model(cr, model)
    # NOTE table_exists is needed to avoid altering views
    if drop_column and table_exists(cr, table) and column_exists(cr, table, fieldname):
        remove_column(cr, table, fieldname, cascade=cascade)


def move_field_to_module(cr, model, fieldname, old_module, new_module):
    name = IMD_FIELD_PATTERN % (model.replace(".", "_"), fieldname)
    try:
        with savepoint(cr):
            cr.execute(
                """
                   UPDATE ir_model_data
                      SET module = %s
                    WHERE model = 'ir.model.fields'
                      AND name = %s
                      AND module = %s
            """,
                [new_module, name, old_module],
            )
    except psycopg2.IntegrityError:
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.model.fields' AND name = %s AND module = %s",
            [name, old_module],
        )


def rename_field(cr, model, old, new, update_references=True):
    rf = ENVIRON["__renamed_fields"].get(model)
    if rf and old in rf:
        rf.discard(old)
        rf.add(new)

    cr.execute("UPDATE ir_model_fields SET name=%s WHERE model=%s AND name=%s RETURNING id", (new, model, old))
    [fid] = cr.fetchone() or [None]
    if fid:
        name = IMD_FIELD_PATTERN % (model.replace(".", "_"), new)
        try:
            with savepoint(cr):
                cr.execute("UPDATE ir_model_data SET name=%s WHERE model='ir.model.fields' AND res_id=%s", [name, fid])
        except psycopg2.IntegrityError:
            # duplicate key. May happen du to conflict between
            # some_model.sub_id and some_model_sub.id
            # (before saas~11.2, where pattern changed)
            name = "%s_%s" % (name, fid)
            cr.execute("UPDATE ir_model_data SET name=%s WHERE model='ir.model.fields' AND res_id=%s", [name, fid])
        cr.execute("UPDATE ir_property SET name=%s WHERE fields_id=%s", [new, fid])

    cr.execute(
        """
       UPDATE ir_translation
          SET name=%s
        WHERE name=%s
          AND type in ('field', 'help', 'model', 'model_terms', 'selection')   -- ignore wizard_* translations
    """,
        ["%s,%s" % (model, new), "%s,%s" % (model, old)],
    )

    if column_exists(cr, "ir_attachment", "res_field"):
        cr.execute(
            """
            UPDATE ir_attachment
               SET res_field = %s
             WHERE res_model = %s
               AND res_field = %s
        """,
            [new, model, old],
        )

    if table_exists(cr, "ir_values"):
        cr.execute(
            """
            UPDATE ir_values
               SET name = %s
             WHERE model = %s
               AND name = %s
               AND key = 'default'
        """,
            [new, model, old],
        )

    if column_type(cr, "mail_tracking_value", "field") == "varchar":
        # From saas~13.1, column `field` is a m2o to the `ir.model.fields`
        cr.execute(
            """
            UPDATE mail_tracking_value v
               SET field = %s
              FROM mail_message m
             WHERE v.mail_message_id = m.id
               AND m.model = %s
               AND v.field = %s
          """,
            [new, model, old],
        )

    table = table_of_model(cr, model)
    # NOTE table_exists is needed to avoid altering views
    if table_exists(cr, table) and column_exists(cr, table, old):
        cr.execute('ALTER TABLE "{}" RENAME COLUMN "{}" TO "{}"'.format(table, old, new))

    if update_references:
        update_field_references(cr, old, new, only_models=(model,))


def convert_field_to_property(
    cr, model, field, type, target_model=None, default_value=None, default_value_ref=None, company_field="company_id"
):
    """
    Notes:
        `target_model` is only use when `type` is "many2one".
        The `company_field` can be an sql expression.
            You may use `t` to refer the model's table.
    """
    type2field = {
        "char": "value_text",
        "float": "value_float",
        "boolean": "value_integer",
        "integer": "value_integer",
        "text": "value_text",
        "binary": "value_binary",
        "many2one": "value_reference",
        "date": "value_datetime",
        "datetime": "value_datetime",
        "selection": "value_text",
    }

    assert type in type2field
    value_field = type2field[type]

    cr.execute("SELECT id FROM ir_model_fields WHERE model=%s AND name=%s", (model, field))
    [fields_id] = cr.fetchone()

    table = table_of_model(cr, model)

    if default_value is None:
        where_clause = "{field} IS NOT NULL".format(field=field)
    else:
        where_clause = "{field} != %(default_value)s".format(field=field)

    if type != "many2one":
        value_select = field
    else:
        # for m2o, the store value is a refrence field, so in format `model,id`
        value_select = "CONCAT('{target_model},', {field})".format(**locals())

    if is_field_anonymized(cr, model, field):
        # if field is anonymized, we need to create a property for each record
        where_clause = "true"
        # and we need to unanonymize its values
        ano_default_value = cr.mogrify("%s", [default_value])
        if type != "many2one":
            ano_value_select = "%(value)s"
        else:
            ano_value_select = "CONCAT('{},', %(value)s)".format(target_model)

        register_unanonymization_query(
            cr,
            model,
            field,
            """
            UPDATE ir_property
               SET {value_field} = CASE WHEN %(value)s IS NULL THEN {ano_default_value}
                                        ELSE {ano_value_select} END
             WHERE res_id = CONCAT('{model},', %(id)s)
               AND name='{field}'
               AND type='{type}'
               AND fields_id={fields_id}
            """.format(
                **locals()
            ),
        )

    cr.execute(
        """
        WITH cte AS (
            SELECT CONCAT('{model},', id) as res_id, {value_select} as value,
                   ({company_field})::integer as company
              FROM {table} t
             WHERE {where_clause}
        )
        INSERT INTO ir_property(name, type, fields_id, company_id, res_id, {value_field})
            SELECT %(field)s, %(type)s, %(fields_id)s, cte.company, cte.res_id, cte.value
              FROM cte
             WHERE NOT EXISTS(SELECT 1
                                FROM ir_property
                               WHERE fields_id=%(fields_id)s
                                 AND COALESCE(company_id, 0) = COALESCE(cte.company, 0)
                                 AND res_id=cte.res_id)
    """.format(
            **locals()
        ),
        locals(),
    )
    # default property
    if default_value:
        cr.execute(
            """
                INSERT INTO ir_property(name, type, fields_id, {value_field})
                     VALUES (%s, %s, %s, %s)
                  RETURNING id
            """.format(
                value_field=value_field
            ),
            (field, type, fields_id, default_value),
        )
        [prop_id] = cr.fetchone()
        if default_value_ref:
            module, _, xid = default_value_ref.partition(".")
            cr.execute(
                """
                    INSERT INTO ir_model_data(module, name, model, res_id, noupdate)
                         VALUES (%s, %s, %s, %s, %s)
            """,
                [module, xid, "ir.property", prop_id, True],
            )

    remove_column(cr, table, field, cascade=True)


def convert_binary_field_to_attachment(cr, model, field, encoded=True):
    table = table_of_model(cr, model)
    if not column_exists(cr, table, field):
        return
    att_name = "%s(%%s).%s" % (model.title().replace(".", ""), field)
    A = env(cr)["ir.attachment"]
    iter_cur = cr._cnx.cursor("fetch_binary")
    iter_cur.itersize = 1
    iter_cur.execute('SELECT id, "{field}" FROM {table} WHERE "{field}" IS NOT NULL'.format(**locals()))
    for rid, data in iter_cur:
        # we can't save create the attachment with res_model & res_id as it will fail computing
        # `res_name` field for non-loaded models. Store it naked and change it via SQL after.
        data = bytes(data)
        if re.match(br"^\d+ (bytes|[KMG]b)$", data, re.I):
            # badly saved data, no need to create an attachment.
            continue
        if not encoded:
            data = base64.b64encode(data)
        att = A.create({"name": att_name % rid, "datas": data, "type": "binary"})
        cr.execute(
            """
               UPDATE ir_attachment
                  SET res_model = %s,
                      res_id = %s,
                      res_field = %s
                WHERE id = %s
            """,
            [model, rid, field, att.id],
        )

    iter_cur.close()
    # free PG space
    remove_column(cr, table, field)


def is_field_anonymized(cr, model, field):
    if not module_installed(cr, "anonymization"):
        return False
    cr.execute(
        """
            SELECT id
              FROM ir_model_fields_anonymization
             WHERE model_name = %s
               AND field_name = %s
               AND state = 'anonymized'
    """,
        [model, field],
    )
    return bool(cr.rowcount)


def register_unanonymization_query(cr, model, field, query, query_type="sql", sequence=10):
    cr.execute(
        """
            INSERT INTO ir_model_fields_anonymization_migration_fix(
                    target_version, sequence, query_type, model_name, field_name, query
            ) VALUES (%s, %s, %s, %s, %s, %s)
    """,
        [release.major_version, sequence, query_type, model, field, query],
    )


class IndirectReference(collections.namedtuple("IndirectReference", "table res_model res_id res_model_id")):
    def model_filter(self, prefix="", placeholder="%s"):
        if prefix and prefix[-1] != ".":
            prefix += "."
        if self.res_model_id:
            placeholder = "(SELECT id FROM ir_model WHERE model={})".format(placeholder)
            column = self.res_model_id
        else:
            column = self.res_model

        return '{}"{}"={}'.format(prefix, column, placeholder)


IndirectReference.__new__.__defaults__ = (None, None)  # https://stackoverflow.com/a/18348004


def indirect_references(cr, bound_only=False):
    IR = IndirectReference
    each = [
        IR("ir_attachment", "res_model", "res_id"),
        IR("ir_cron", "model", None),
        IR("ir_act_report_xml", "model", None),
        IR("ir_act_window", "res_model", "res_id"),
        IR("ir_act_window", "src_model", None),
        IR("ir_act_server", "wkf_model_name", None),
        IR("ir_act_server", "crud_model_name", None),
        IR("ir_act_client", "res_model", None),
        IR("ir_model", "model", None),
        IR("ir_model_fields", "model", None),
        IR("ir_model_fields", "relation", None),  # destination of a relation field
        IR("ir_model_data", "model", "res_id"),
        IR("ir_filters", "model_id", None),  # YUCK!, not an id
        IR("ir_exports", "resource", None),
        IR("ir_ui_view", "model", None),
        IR("ir_values", "model", "res_id"),
        IR("wkf_transition", "trigger_model", None),
        IR("wkf_triggers", "model", None),
        IR("ir_model_fields_anonymization", "model_name", None),
        IR("ir_model_fields_anonymization_migration_fix", "model_name", None),
        IR("base_import_import", "res_model", None),
        IR("calendar_event", "res_model", "res_id"),  # new in saas~18
        IR("documents_document", "res_model", "res_id"),
        IR("email_template", "model", None),  # stored related
        IR("mail_template", "model", None),  # model renamed in saas~6
        IR("mail_activity", "res_model", "res_id", "res_model_id"),
        IR("mail_alias", None, "alias_force_thread_id", "alias_model_id"),
        IR("mail_alias", None, "alias_parent_thread_id", "alias_parent_model_id"),
        IR("mail_followers", "res_model", "res_id"),
        IR("mail_message_subtype", "res_model", None),
        IR("mail_message", "model", "res_id"),
        IR("mail_compose_message", "model", "res_id"),
        IR("mail_wizard_invite", "res_model", "res_id"),
        IR("mail_mail_statistics", "model", "res_id"),
        IR("mailing_trace", "model", "res_id"),
        IR("mail_mass_mailing", "mailing_model", None, "mailing_model_id"),
        IR("mailing_mailing", None, None, "mailing_model_id"),
        IR("project_project", "alias_model", None),
        IR("rating_rating", "res_model", "res_id", "res_model_id"),
        IR("rating_rating", "parent_res_model", "parent_res_id", "parent_res_model_id"),
    ]

    for ir in each:
        if bound_only and not ir.res_id:
            continue
        if ir.res_id and not column_exists(cr, ir.table, ir.res_id):
            continue

        # some `res_model/res_model_id` combination may change between
        # versions (i.e. rating_rating.res_model_id was added in saas~15).
        # we need to verify existance of columns before using them.
        if ir.res_model and not column_exists(cr, ir.table, ir.res_model):
            ir = ir._replace(res_model=None)
        if ir.res_model_id and not column_exists(cr, ir.table, ir.res_model_id):
            ir = ir._replace(res_model_id=None)
        if not ir.res_model and not ir.res_model_id:
            continue

        yield ir


def res_model_res_id(cr, filtered=True):
    for ir in indirect_references(cr):
        if ir.res_model:
            yield model_of_table(cr, ir.table), ir.res_model, ir.res_id


def _ir_values_value(cr):
    # returns the casting from bytea to text needed in saas~17 for column `value` of `ir_values`
    # returns tuple(column_read, cast_write)
    result = getattr(_ir_values_value, "result", None)

    if result is None:
        if column_type(cr, "ir_values", "value") == "bytea":
            cr.execute("SELECT character_set_name FROM information_schema.character_sets")
            (charset,) = cr.fetchone()
            column_read = "convert_from(value, '%s')" % charset
            cast_write = "convert_to(%%s, '%s')" % charset
        else:
            column_read = "value"
            cast_write = "%s"
        _ir_values_value.result = result = (column_read, cast_write)

    return result


def _rm_refs(cr, model, ids=None):
    if ids is None:
        match = "like %s"
        needle = model + ",%"
    else:
        if not ids:
            return
        match = "in %s"
        needle = tuple("{},{}".format(model, i) for i in ids)

    # "model-comma" fields
    cr.execute(
        """
        SELECT model, name
          FROM ir_model_fields
         WHERE ttype='reference'
         UNION
        SELECT 'ir.translation', 'name'
    """
    )

    for ref_model, ref_column in cr.fetchall():
        table = table_of_model(cr, ref_model)
        # NOTE table_exists is needed to avoid deleting from views
        if table_exists(cr, table) and column_exists(cr, table, ref_column):
            query_tail = ' FROM "{}" WHERE "{}" {}'.format(table, ref_column, match)
            if ref_model == "ir.ui.view":
                cr.execute("SELECT id" + query_tail, [needle])
                for (view_id,) in cr.fetchall():
                    remove_view(cr, view_id=view_id, silent=True)
            elif ref_model == "ir.ui.menu":
                cr.execute("SELECT id" + query_tail, [needle])
                menu_ids = tuple(m[0] for m in cr.fetchall())
                remove_menus(cr, menu_ids)
            else:
                cr.execute("SELECT id" + query_tail, [needle])
                for (record_id,) in cr.fetchall():
                    remove_record(cr, (ref_model, record_id))

    if table_exists(cr, "ir_values"):
        column, _ = _ir_values_value(cr)
        query = "DELETE FROM ir_values WHERE {} {}".format(column, match)
        cr.execute(query, [needle])

    if ids is None:
        cr.execute(
            """
            DELETE FROM ir_translation
             WHERE name=%s
               AND type IN ('constraint', 'sql_constraint', 'view', 'report', 'rml', 'xsl')
        """,
            [model],
        )


def remove_model(cr, model, drop_table=True):
    model_underscore = model.replace(".", "_")

    # remove references
    for ir in indirect_references(cr):
        if ir.table in ("ir_model", "ir_model_fields"):
            continue
        if ir.table == "ir_ui_view":
            cr.execute("SELECT id FROM ir_ui_view WHERE {}".format(ir.model_filter()), [model])
            for (view_id,) in cr.fetchall():
                remove_view(cr, view_id=view_id, silent=True)
        else:
            query = 'DELETE FROM "{}" WHERE {} RETURNING id'.format(ir.table, ir.model_filter())
            cr.execute(query, [model])
            chunk_size = 1000
            size = (cr.rowcount + chunk_size - 1) / chunk_size
            for ids in log_progress(
                chunks(map(itemgetter(0), cr.fetchall()), chunk_size, fmt=tuple), qualifier=ir.table, size=size
            ):
                _rm_refs(cr, model_of_table(cr, ir.table), ids)

    _rm_refs(cr, model)

    cr.execute("SELECT id FROM ir_model WHERE model=%s", (model,))
    [mod_id] = cr.fetchone() or [None]
    if mod_id:
        # some required fk are in "ON DELETE SET NULL/RESTRICT".
        for tbl in "base_action_rule base_automation google_drive_config".split():
            if column_exists(cr, tbl, "model_id"):
                cr.execute("DELETE FROM {} WHERE model_id=%s".format(tbl), [mod_id])
        cr.execute("DELETE FROM ir_model_constraint WHERE model=%s", (mod_id,))
        cr.execute("DELETE FROM ir_model_relation WHERE model=%s", (mod_id,))

        # Drop XML IDs of ir.rule and ir.model.access records that will be cascade-dropped,
        # when the ir.model record is dropped - just in case they need to be re-created
        cr.execute(
            """
                DELETE
                  FROM ir_model_data x
                 USING ir_rule a
                 WHERE x.res_id = a.id
                   AND x.model = 'ir.rule'
                   AND a.model_id = %s
        """,
            [mod_id],
        )
        cr.execute(
            """
                DELETE
                  FROM ir_model_data x
                 USING ir_model_access a
                 WHERE x.res_id = a.id
                   AND x.model = 'ir.model.access'
                   AND a.model_id = %s
        """,
            [mod_id],
        )

        cr.execute("DELETE FROM ir_model WHERE id=%s", (mod_id,))

    cr.execute("DELETE FROM ir_model_data WHERE model=%s AND name=%s", ("ir.model", "model_%s" % model_underscore))
    cr.execute(
        "DELETE FROM ir_model_data WHERE model='ir.model.fields' AND name LIKE %s",
        [(IMD_FIELD_PATTERN % (model_underscore, "%")).replace("_", r"\_")],
    )

    table = table_of_model(cr, model)
    if drop_table:
        if table_exists(cr, table):
            cr.execute('DROP TABLE "{}" CASCADE'.format(table))
        elif view_exists(cr, table):
            cr.execute('DROP VIEW "{}" CASCADE'.format(table))


# compat layer...
delete_model = remove_model


def move_model(cr, model, from_module, to_module, move_data=False):
    """
    move model `model` from `from_module` to `to_module`.
    if `to_module` is not installed, delete the model.
    """
    if not module_installed(cr, to_module):
        delete_model(cr, model)
        return

    def update_imd(model, name=None, from_module=from_module, to_module=to_module):
        where = "true"
        if name:
            where = "d.name {} %(name)s".format("LIKE" if "%" in name else "=")

        query = """
            WITH dups AS (
                SELECT d.id
                  FROM ir_model_data d, ir_model_data t
                 WHERE d.name = t.name
                   AND d.module = %(from_module)s
                   AND t.module = %(to_module)s
                   AND d.model = %(model)s
                   AND {}
            )
            DELETE FROM ir_model_data d
                  USING dups
                 WHERE dups.id = d.id
        """
        cr.execute(query.format(where), locals())

        query = """
            UPDATE ir_model_data d
               SET module = %(to_module)s
             WHERE module = %(from_module)s
               AND model = %(model)s
               AND {}
        """
        cr.execute(query.format(where), locals())

    model_u = model.replace(".", "_")

    update_imd("ir.model", "model_%s" % model_u)
    update_imd("ir.model.fields", (IMD_FIELD_PATTERN % (model_u, "%")).replace("_", r"\_"))
    if move_data:
        update_imd(model)
    return


def rename_model(cr, old, new, rename_table=True):
    if old in ENVIRON["__renamed_fields"]:
        ENVIRON["__renamed_fields"][new] = ENVIRON["__renamed_fields"].pop(old)
    if rename_table:
        old_table = table_of_model(cr, old)
        new_table = table_of_model(cr, new)
        cr.execute('ALTER TABLE "{}" RENAME TO "{}"'.format(old_table, new_table))
        cr.execute('ALTER SEQUENCE "{}_id_seq" RENAME TO "{}_id_seq"'.format(old_table, new_table))

        # update moved0 references
        ENVIRON["moved0"] = {(new_table if t == old_table else t, c) for t, c in ENVIRON.get("moved0", ())}

        # find & rename primary key, may still use an old name from a former migration
        cr.execute(
            """
            SELECT conname
              FROM pg_index, pg_constraint
             WHERE indrelid = %s::regclass
               AND indisprimary
               AND conrelid = indrelid
               AND conindid = indexrelid
               AND confrelid = 0
        """,
            [new_table],
        )
        (primary_key,) = cr.fetchone()
        cr.execute('ALTER INDEX "{}" RENAME TO "{}_pkey"'.format(primary_key, new_table))

        # DELETE all constraints and indexes (ignore the PK), ORM will recreate them.
        cr.execute(
            """
                SELECT constraint_name
                  FROM information_schema.table_constraints
                 WHERE table_name = %s
                   AND constraint_type != %s
                   AND constraint_name !~ '^[0-9_]+_not_null$'
        """,
            [new_table, "PRIMARY KEY"],
        )
        for (const,) in cr.fetchall():
            cr.execute("DELETE FROM ir_model_constraint WHERE name=%s", (const,))
            cr.execute('ALTER TABLE "{}" DROP CONSTRAINT "{}"'.format(new_table, const))

    updates = [("wkf", "osv")] if table_exists(cr, "wkf") else []
    updates += [r[:2] for r in res_model_res_id(cr)]

    for model, column in updates:
        table = table_of_model(cr, model)
        query = "UPDATE {t} SET {c}=%s WHERE {c}=%s".format(t=table, c=column)
        cr.execute(query, (new, old))

    # "model-comma" fields
    cr.execute(
        """
        SELECT model, name
          FROM ir_model_fields
         WHERE ttype='reference'
    """
    )
    for model, column in cr.fetchall():
        table = table_of_model(cr, model)
        if column_exists(cr, table, column):
            cr.execute(
                """
                    UPDATE "{table}"
                       SET {column}='{new}' || substring({column} FROM '%#",%#"' FOR '#')
                     WHERE {column} LIKE '{old},%'
            """.format(
                    table=table, column=column, new=new, old=old
                )
            )

    # translations
    cr.execute(
        """
        WITH renames AS (
            SELECT id, name, '{new}' || substring(name FROM '%#",%#"' FOR '#') as new
              FROM ir_translation
             WHERE name LIKE '{old},%'
        )
        UPDATE ir_translation t
           SET name = r.new
          FROM renames r
     LEFT JOIN ir_translation e ON (e.name = r.new)
         WHERE t.name = r.name
           AND e.id IS NULL
    """.format(
            new=new, old=old
        )
    )
    cr.execute("DELETE FROM ir_translation WHERE name LIKE '{},%'".format(old))

    if table_exists(cr, "ir_values"):
        column_read, cast_write = _ir_values_value(cr)
        query = """
            UPDATE ir_values
               SET value = {cast[0]}'{new}' || substring({column} FROM '%#",%#"' FOR '#'){cast[2]}
             WHERE {column} LIKE '{old},%'
        """.format(
            column=column_read, new=new, old=old, cast=cast_write.partition("%s")
        )
        cr.execute(query)

    cr.execute(
        """
        UPDATE ir_translation
           SET name=%s
         WHERE name=%s
           AND type IN ('constraint', 'sql_constraint', 'view', 'report', 'rml', 'xsl')
    """,
        [new, old],
    )
    old_u = old.replace(".", "_")
    new_u = new.replace(".", "_")

    cr.execute(
        "UPDATE ir_model_data SET name=%s WHERE model=%s AND name=%s",
        ("model_%s" % new_u, "ir.model", "model_%s" % old_u),
    )

    cr.execute(
        """
            UPDATE ir_model_data
               SET name=%s || substring(name from %s)
             WHERE model='ir.model.fields'
               AND name LIKE %s
    """,
        ["field_%s" % new_u, len(old_u) + 7, (IMD_FIELD_PATTERN % (old_u, "%")).replace("_", r"\_")],
    )

    col_prefix = ""
    if not column_exists(cr, "ir_act_server", "condition"):
        col_prefix = "--"  # sql comment the line

    cr.execute(
        r"""
        UPDATE ir_act_server
           SET {col_prefix} condition=regexp_replace(condition, '([''"]){old}\1', '\1{new}\1', 'g'),
               code=regexp_replace(code, '([''"]){old}\1', '\1{new}\1', 'g')
    """.format(
            col_prefix=col_prefix, old=old.replace(".", r"\."), new=new
        )
    )


def remove_mixin_from_model(cr, model, mixin, keep=()):
    assert env(cr)[mixin]._abstract
    cr.execute(
        """
        SELECT name, ttype, relation, store
          FROM ir_model_fields
         WHERE model = %s
           AND name NOT IN ('id',
                            'create_uid', 'write_uid',
                            'create_date', 'write_date',
                            '__last_update', 'display_name')
           AND name != ALL(%s)
    """,
        [mixin, list(keep)],
    )
    for field, ftype, relation, store in cr.fetchall():
        if ftype.endswith("2many") and store:
            # for mixin, x2many are filtered by their model.
            table = table_of_model(cr, relation)
            irs = [ir for ir in indirect_references(cr) if ir.table == table]
            assert irs  # something goes wrong...
            for ir in irs:
                query = 'DELETE FROM "{}" WHERE {}'.format(ir.table, ir.model_filter())
                cr.execute(query, [model])
        remove_field(cr, model, field)


def replace_record_references(cr, old, new, replace_xmlid=True):
    """replace all (in)direct references of a record by another"""
    # TODO update workflow instances?
    assert isinstance(old, tuple) and len(old) == 2
    assert isinstance(new, tuple) and len(new) == 2

    if not old[1]:
        return

    return replace_record_references_batch(cr, {old[1]: new[1]}, old[0], new[0], replace_xmlid)


def replace_record_references_batch(cr, id_mapping, model_src, model_dst=None, replace_xmlid=True):
    assert id_mapping
    assert all(isinstance(v, int) and isinstance(k, int) for k, v in id_mapping.items())

    if model_dst is None:
        model_dst = model_src

    old = tuple(id_mapping.keys())
    new = tuple(id_mapping.values())
    jmap = json.dumps(id_mapping)

    def genmap(fmt_k, fmt_v=None):
        # generate map using given format
        fmt_v = fmt_k if fmt_v is None else fmt_v
        m = {fmt_k % k: fmt_v % v for k, v in id_mapping.items()}
        return json.dumps(m), tuple(m.keys())

    if model_src == model_dst:
        pmap, pmap_keys = genmap("I%d\n.")  # 7 time faster than using pickle.dumps
        smap, smap_keys = genmap("%d")

        column_read, cast_write = _ir_values_value(cr)

        for table, fk, _, _ in get_fk(cr, table_of_model(cr, model_src)):
            query = """
                UPDATE {table} t
                   SET {fk} = ('{jmap}'::json->>{fk}::varchar)::int4
                 WHERE {fk} IN %(old)s
            """

            col2 = None
            if not column_exists(cr, table, "id"):
                # seems to be a m2m table. Avoid duplicated entries
                cols = get_columns(cr, table, ignore=(fk,))[0]
                assert len(cols) == 1  # it's a m2, should have only 2 columns
                col2 = cols[0]
                query = (
                    """
                    WITH _existing AS (
                        SELECT {col2} FROM {table} WHERE {fk} IN %%(new)s
                    )
                    %s
                    AND NOT EXISTS(SELECT 1 FROM _existing WHERE {col2}=t.{col2});
                    DELETE FROM {table} WHERE {fk} IN %%(old)s;
                """
                    % query
                )

            cr.execute(query.format(table=table, fk=fk, jmap=jmap, col2=col2), dict(new=new, old=old))

            if not col2:  # it's a model
                # update default values
                # TODO? update all defaults using 1 query (using `WHERE (model, name) IN ...`)
                model = model_of_table(cr, table)
                if table_exists(cr, "ir_values"):
                    query = """
                         UPDATE ir_values
                            SET value = {cast[0]} '{pmap}'::json->>({col}) {cast[2]}
                          WHERE key='default'
                            AND model=%s
                            AND name=%s
                            AND {col} IN %s
                    """.format(
                        col=column_read, cast=cast_write.partition("%s"), pmap=pmap
                    )
                    cr.execute(query, [model, fk, pmap_keys])
                else:
                    cr.execute(
                        """
                        UPDATE ir_default d
                           SET json_value = '{smap}'::json->>json_value
                          FROM ir_model_fields f
                         WHERE f.id = d.field_id
                           AND f.model = %s
                           AND f.name = %s
                           AND d.json_value IN %s
                    """.format(
                            smap=smap
                        ),
                        [model, fk, smap_keys],
                    )

    # indirect references
    for ir in indirect_references(cr, bound_only=True):
        if ir.table == "ir_model_data" and not replace_xmlid:
            continue
        upd = ""
        if ir.res_model:
            upd += '"{ir.res_model}" = %(model_dst)s,'
        if ir.res_model_id:
            upd += '"{ir.res_model_id}" = (SELECT id FROM ir_model WHERE model=%(model_dst)s),'
        upd = upd.format(ir=ir)
        whr = ir.model_filter(placeholder="%(model_src)s")

        query = """
            UPDATE "{ir.table}"
               SET {upd}
                   "{ir.res_id}" = ('{jmap}'::json->>{ir.res_id}::varchar)::int4
             WHERE {whr}
               AND {ir.res_id} IN %(old)s
        """.format(
            **locals()
        )

        cr.execute(query, locals())

    # reference fields
    cmap, cmap_keys = genmap("%s,%%d" % model_src, "%s,%%d" % model_dst)
    cr.execute("SELECT model, name FROM ir_model_fields WHERE ttype='reference'")
    for model, column in cr.fetchall():
        table = table_of_model(cr, model)
        if column_exists(cr, table, column):
            cr.execute(
                """
                    UPDATE "{table}"
                       SET "{column}" = '{cmap}'::json->>"{column}"
                     WHERE "{column}" IN %s
            """.format(
                    table=table, column=column, cmap=cmap
                ),
                [cmap_keys],
            )


def update_field_references(cr, old, new, only_models=None):
    """
    Replace all references to field `old` to `new` in:
        - ir_filters
        - ir_exports_line
        - ir_act_server
        - ir_rule
        - mail_mass_mailing
        - mail_alias
    """
    p = {
        "old": r"\y%s\y" % (old,),
        "new": new,
        "def_old": r"\ydefault_%s\y" % (old,),
        "def_new": "default_%s" % (new,),
        "models": tuple(only_models) if only_models else (),
    }

    col_prefix = ""
    if not column_exists(cr, "ir_filters", "sort"):
        col_prefix = "--"  # sql comment the line
    q = """
        UPDATE ir_filters
           SET {col_prefix} sort = regexp_replace(sort, %(old)s, %(new)s, 'g'),
               domain = regexp_replace(domain, %(old)s, %(new)s, 'g'),
               context = regexp_replace(regexp_replace(context,
                                                       %(old)s, %(new)s, 'g'),
                                                       %(def_old)s, %(def_new)s, 'g')
    """

    if only_models:
        q += " WHERE model_id IN %(models)s AND "
    else:
        q += " WHERE "
    q += """
        (
            domain ~ %(old)s
            OR context ~ %(old)s
            OR context ~ %(def_old)s
            {col_prefix} OR sort ~ %(old)s
        )
    """
    cr.execute(q.format(col_prefix=col_prefix), p)

    # ir.exports.line
    q = """
        UPDATE ir_exports_line l
           SET name = regexp_replace(l.name, %(old)s, %(new)s, 'g')
    """
    if only_models:
        q += """
          FROM ir_exports e
         WHERE e.id = l.export_id
           AND e.resource IN %(models)s
           AND
        """
    else:
        q += "WHERE "
    q += "l.name ~ %(old)s"
    cr.execute(q, p)

    # ir.action.server
    col_prefix = ""
    if not column_exists(cr, "ir_act_server", "condition"):
        col_prefix = "--"  # sql comment the line
    q = """
        UPDATE ir_act_server s
           SET {col_prefix} condition = regexp_replace(condition, %(old)s, %(new)s, 'g'),
               code = regexp_replace(code, %(old)s, %(new)s, 'g')
    """
    if only_models:
        q += """
          FROM ir_model m
         WHERE m.id = s.model_id
           AND m.model IN %(models)s
           AND
        """
    else:
        q += " WHERE "

    q += """s.state = 'code'
           AND (
              s.code ~ %(old)s
              {col_prefix} OR s.condition ~ %(old)s
           )
    """
    cr.execute(q.format(col_prefix=col_prefix), p)

    # ir.rule
    q = """
        UPDATE ir_rule r
           SET domain_force = regexp_replace(domain_force, %(old)s, %(new)s, 'g')
    """
    if only_models:
        q += """
          FROM ir_model m
         WHERE m.id = r.model_id
           AND m.model IN %(models)s
           AND
        """
    else:
        q += "WHERE "
    q += "r.domain_force ~ %(old)s"
    cr.execute(q, p)

    # mass mailing
    ml_table = "mailing_mailing" if table_exists(cr, "mailing_mailing") else "mail_mass_mailing"
    if column_exists(cr, ml_table, "mailing_domain"):
        q = """
            UPDATE {} u
               SET mailing_domain = regexp_replace(u.mailing_domain, %(old)s, %(new)s, 'g')
        """
        if only_models:
            if column_exists(cr, ml_table, "mailing_model_id"):
                q += """
                  FROM ir_model m
                 WHERE m.id = u.mailing_model_id
                   AND m.model IN %(models)s
                   AND
                """
            else:
                q += "WHERE u.mailing_model IN %(models)s AND "
        else:
            q += "WHERE "
        q += "u.mailing_domain ~ %(old)s"
        cr.execute(q.format(ml_table), p)

    # mail.alias
    if column_exists(cr, "mail_alias", "alias_defaults"):
        q = """
            UPDATE mail_alias a
               SET alias_defaults = regexp_replace(a.alias_defaults, %(old)s, %(new)s, 'g')
        """
        if only_models:
            q += """
              FROM ir_model m
             WHERE m.id = a.alias_model_id
               AND m.model IN %(models)s
               AND
            """
        else:
            q += "WHERE "
        q += "a.alias_defaults ~ %(old)s"
        cr.execute(q, p)


def recompute_fields(cr, model, fields, ids=None, logger=_logger, chunk_size=256):
    Model = env(cr)[model] if isinstance(model, basestring) else model
    model = Model._name
    if ids is None:
        cr.execute('SELECT id FROM "%s"' % table_of_model(cr, model))
        ids = tuple(map(itemgetter(0), cr.fetchall()))

    size = (len(ids) + chunk_size - 1) / chunk_size
    qual = "%s %d-bucket" % (model, chunk_size) if chunk_size != 1 else model
    for subids in log_progress(chunks(ids, chunk_size, list), qualifier=qual, logger=logger, size=size):
        records = Model.browse(subids)
        for field_name in fields:
            field = records._fields[field_name]
            if hasattr(records, "_recompute_todo"):
                # < 13.0
                records._recompute_todo(field)
            else:
                Model.env.add_to_compute(field, records)
        records.recompute()
        records.invalidate_cache()


def check_company_fields(
    cr, model_name, field_name, logger=_logger, model_company_field="company_id", comodel_company_field="company_id"
):
    cr.execute(
        """
            SELECT *
              FROM ir_model_fields
             WHERE name = %s
               AND model = %s
               AND store IS TRUE
               AND ttype IN ('many2one', 'many2many')
    """,
        [field_name, model_name],
    )

    field_values = cr.dictfetchone()

    if not field_values:
        _logger.warning("Field %s not found on model %s.", field_name, model_name)
        return

    table_model_1 = table_of_model(cr, model_name)
    table_model_2 = table_of_model(cr, field_values["relation"])
    if field_values["ttype"] == "many2one":
        cr.execute(
            """
            SELECT
                record1.id                                  AS id_1,
                record1.%(comp_field_1)s                    AS comp_1,
                record2.id                                  AS id_2,
                record2.%(comp_field_2)s                    AS comp_2
            FROM %(table_model)s record1
            JOIN %(table_relation)s record2 ON record2.id = record1.%(cofield_name)s
            WHERE record1.%(comp_field_1)s IS NOT NULL
            AND record2.%(comp_field_2)s IS NOT NULL
            AND record1.%(comp_field_1)s != record2.%(comp_field_2)s
        """
            % {
                "comp_field_1": model_company_field,
                "comp_field_2": comodel_company_field,
                "table_model": table_model_1,
                "table_relation": table_model_2,
                "cofield_name": field_values["name"],
            }
        )
    else:  # if field_values['ttype'] == 'many2many'
        cr.execute(
            """
            SELECT
                record1.id                                  AS id_1,
                record1.%(comp_field_1)s                    AS comp_1,
                record2.id                                  AS id_2,
                record2.%(comp_field_2)s                    AS comp_2
            FROM %(table_rel)s rel
            JOIN %(table_model)s record1 ON record1.id = rel.%(column1)s
            JOIN %(table_relation)s record2 ON record2.id = rel.%(column2)s
            WHERE record1.%(comp_field_1)s IS NOT NULL
            AND record2.%(comp_field_2)s IS NOT NULL
            AND record1.%(comp_field_1)s != record2.%(comp_field_2)s
        """
            % {
                "comp_field_1": comodel_company_field,
                "comp_field_2": model_company_field,
                "table_rel": field_values["relation_table"],
                "table_model": table_model_1,
                "table_relation": table_model_2,
                "column1": field_values["column1"],
                "column2": field_values["column2"],
            }
        )

    for res in cr.fetchall():
        logger.warning(
            "Company fields are not consistent on models %s "
            "(id=%s, company_id=%s) and %s (id=%s, company_id=%s) "
            "through relation %s (%s)",
            table_model_1,
            res[0],
            res[1],
            table_model_2,
            res[2],
            res[3],
            field_values["name"],
            field_values["ttype"],
        )


def split_group(cr, from_groups, to_group):
    """Users have all `from_groups` will be added into `to_group`"""

    def check_group(g):
        if isinstance(g, basestring):
            gid = ref(cr, g)
            if not gid:
                _logger.warning("split_group(): Unknow group: %r", g)
            return gid
        return g

    if not isinstance(from_groups, (list, tuple, set)):
        from_groups = [from_groups]

    from_groups = [g for g in map(check_group, from_groups) if g]
    if not from_groups:
        return

    if isinstance(to_group, basestring):
        to_group = ref(cr, to_group)

    assert to_group

    cr.execute(
        """
        INSERT INTO res_groups_users_rel(uid, gid)
             SELECT uid, %s
               FROM res_groups_users_rel
           GROUP BY uid
             HAVING array_agg(gid) @> %s
             EXCEPT
             SELECT uid, gid
               FROM res_groups_users_rel
              WHERE gid = %s
    """,
        [to_group, from_groups, to_group],
    )


def rst2html(rst):
    overrides = dict(embed_stylesheet=False, doctitle_xform=False, output_encoding="unicode", xml_declaration=False)
    html = publish_string(source=dedent(rst), settings_overrides=overrides, writer=MyWriter())
    return html_sanitize(html, silent=False)


def md2html(md):
    mdversion = markdown.__version_info__ if hasattr(markdown, "__version_info__") else markdown.version_info
    extensions = [
        "markdown.extensions.nl2br",
        "markdown.extensions.sane_lists",
    ]
    if mdversion[0] < 3:
        extensions.append("markdown.extensions.smart_strong")

    return markdown.markdown(md, extensions=extensions)


_DEFAULT_HEADER = """
<p>Odoo has been upgraded to version {version}.</p>
<h2>What's new in this upgrade?</h2>
"""

_DEFAULT_FOOTER = "<p>Enjoy the new Odoo Online!</p>"

_DEFAULT_RECIPIENT = "mail.%s_all_employees" % ["group", "channel"][version_gte("9.0")]


def announce(
    cr,
    version,
    msg,
    format="rst",
    recipient=_DEFAULT_RECIPIENT,
    header=_DEFAULT_HEADER,
    footer=_DEFAULT_FOOTER,
    pluses_for_enterprise=None,
):

    if pluses_for_enterprise is None:
        # default value depend on format and version
        major = version[0]
        pluses_for_enterprise = (major == "s" or int(major) >= 9) and format == "md"

    if pluses_for_enterprise:
        plus_re = r"^(\s*)\+ (.+)\n"
        replacement = r"\1- \2\n" if has_enterprise() else ""
        msg = re.sub(plus_re, replacement, msg, flags=re.M)

    # do not notify early, in case the migration fails halfway through
    ctx = {"mail_notify_force_send": False, "mail_notify_author": True}

    uid = guess_admin_id(cr)
    try:
        registry = env(cr)
        user = registry["res.users"].browse([uid])[0].with_context(ctx)

        def ref(xid):
            return registry.ref(xid).with_context(ctx)

    except MigrationError:
        try:
            from openerp.modules.registry import RegistryManager
        except ImportError:
            from openerp.modules.registry import Registry as RegistryManager
        registry = RegistryManager.get(cr.dbname)
        user = registry["res.users"].browse(cr, SUPERUSER_ID, uid, context=ctx)

        def ref(xid):
            rmod, _, rxid = recipient.partition(".")
            return registry["ir.model.data"].get_object(cr, SUPERUSER_ID, rmod, rxid, context=ctx)

    # default recipient
    poster = user.message_post if hasattr(user, "message_post") else user.partner_id.message_post

    if recipient:
        try:
            if isinstance(recipient, str):
                recipient = ref(recipient)
            else:
                recipient = recipient.with_context(**ctx)
            poster = recipient.message_post
        except (ValueError, AttributeError):
            # Cannot find record, post the message on the wall of the admin
            pass

    if format == "rst":
        msg = rst2html(msg)
    elif format == "md":
        msg = md2html(msg)

    message = ((header or "") + msg + (footer or "")).format(version=version)
    _logger.debug(message)

    type_field = ["type", "message_type"][version_gte("9.0")]
    # From 12.0, system notificatications are sent by email,
    # and do not increment the upper right notification counter.
    # While comments, in a mail.channel, do.
    # We want the notification counter to appear for announcements, so we force the comment type from 12.0.
    type_value = ["notification", "comment"][version_gte("12.0")]
    subtype_key = ["subtype", "subtype_xmlid"][version_gte("saas~13.1")]

    kw = {type_field: type_value, subtype_key: "mail.mt_comment"}

    try:
        poster(body=message, partner_ids=[user.partner_id.id], **kw)
    except Exception:
        _logger.warning("Cannot announce message", exc_info=True)


def announce_migration_report(cr):
    filepath = os.path.join(os.path.dirname(__file__), "report-migration.xml")
    with open(filepath, "r") as fp:
        report = lxml.etree.fromstring(fp.read())
    e = env(cr)
    values = {
        "action_view_id": e.ref("base.action_ui_view").id,
        "major_version": release.major_version,
        "messages": migration_reports,
    }
    _logger.info(migration_reports)
    message = e["ir.qweb"].render(report, values=values).decode()
    if message.strip():
        kw = {}
        # If possible, post the migration report message to administrators only.
        admin_channel = get_admin_channel(cr)
        if admin_channel:
            kw["recipient"] = admin_channel
        announce(cr, release.major_version, message, format="html", header=None, footer=None, **kw)


def get_admin_channel(cr):
    e = env(cr)
    admin_channel = None
    if "mail.channel" in e:
        admin_group = e.ref("base.group_system", raise_if_not_found=False)
        if admin_group:
            admin_channel = e["mail.channel"].search(
                [
                    ("public", "=", "groups"),
                    ("group_public_id", "=", admin_group.id),
                    ("group_ids", "in", admin_group.id),
                ]
            )
            if not admin_channel:
                admin_channel = e["mail.channel"].create(
                    {
                        "name": "Administrators",
                        "public": "groups",
                        "group_public_id": admin_group.id,
                        "group_ids": [(6, 0, [admin_group.id])],
                    }
                )
    return admin_channel


def guess_admin_id(cr):
    """guess the admin user id of `cr` database"""
    if not version_gte("12.0"):
        return SUPERUSER_ID
    cr.execute(
        """
        SELECT min(r.uid)
          FROM res_groups_users_rel r
          JOIN res_users u ON r.uid = u.id
         WHERE u.active
           AND r.gid = (SELECT res_id
                          FROM ir_model_data
                         WHERE module = 'base'
                           AND name = 'group_system')
    """,
        [SUPERUSER_ID],
    )
    return cr.fetchone()[0] or SUPERUSER_ID


def drop_workflow(cr, osv):
    if not table_exists(cr, "wkf"):
        # workflows have been removed in 10.saas~14
        # noop if there is no workflow tables anymore...
        return

    cr.execute(
        """
        -- we want to first drop the foreign keys on the workitems because
        -- it slows down the process a lot
        ALTER TABLE wkf_triggers DROP CONSTRAINT wkf_triggers_workitem_id_fkey;
        ALTER TABLE wkf_workitem DROP CONSTRAINT wkf_workitem_act_id_fkey;
        ALTER TABLE wkf_workitem DROP CONSTRAINT wkf_workitem_inst_id_fkey;
        ALTER TABLE wkf_triggers DROP CONSTRAINT wkf_triggers_instance_id_fkey;
        -- if this workflow is used as a subflow, complete workitem running this subflow
        UPDATE wkf_workitem wi
           SET state = 'complete'
          FROM wkf_instance i JOIN wkf w ON (w.id = i.wkf_id)
         WHERE wi.subflow_id = i.id
           AND w.osv = %(osv)s
           AND wi.state = 'running'
        ;
        -- delete the workflow and dependencies
        WITH deleted_wkf AS (
            DELETE FROM wkf WHERE osv = %(osv)s RETURNING id
        ),
        deleted_wkf_instance AS (
            DELETE FROM wkf_instance i
                  USING deleted_wkf w
                  WHERE i.wkf_id = w.id
              RETURNING i.id
        ),
        _delete_triggers AS (
            DELETE FROM wkf_triggers t
                  USING deleted_wkf_instance i
                  WHERE t.instance_id = i.id
        ),
        deleted_wkf_activity AS (
            DELETE FROM wkf_activity a
                  USING deleted_wkf w
                  WHERE a.wkf_id = w.id
              RETURNING a.id
        )
        DELETE FROM wkf_workitem wi
              USING deleted_wkf_instance i
              WHERE wi.inst_id = i.id
        ;
        -- recreate constraints
        ALTER TABLE wkf_triggers ADD CONSTRAINT wkf_triggers_workitem_id_fkey
            FOREIGN KEY (workitem_id) REFERENCES wkf_workitem(id)
            ON DELETE CASCADE;
        ALTER TABLE wkf_workitem ADD CONSTRAINT wkf_workitem_act_id_fkey
            FOREIGN key (act_id) REFERENCES wkf_activity(id)
            ON DELETE CASCADE;
        ALTER TABLE wkf_workitem ADD CONSTRAINT wkf_workitem_inst_id_fkey
            FOREIGN KEY (inst_id) REFERENCES wkf_instance(id)
            ON DELETE CASCADE;
        ALTER TABLE wkf_triggers ADD CONSTRAINT wkf_triggers_instance_id_fkey
            FOREIGN KEY (instance_id) REFERENCES wkf_instance(id)
            ON DELETE CASCADE;
        """,
        dict(osv=osv),
    )


def chunks(iterable, size, fmt=None):
    """
    Split `iterable` into chunks of `size` and wrap each chunk
    using function 'fmt' (`iter` by default; join strings)
    >>> list(chunks(range(10), 4, fmt=tuple))
    [(0, 1, 2, 3), (4, 5, 6, 7), (8, 9)]
    >>> ' '.join(chunks('abcdefghijklm', 3))
    'abc def ghi jkl m'
    >>>
    """
    if fmt is None:
        # fmt:off
        fmt = {
            str: "".join,
            unicode: u"".join,
        }.get(type(iterable), iter)
        # fmt:on

    it = iter(iterable)
    try:
        while True:
            yield fmt(chain((next(it),), islice(it, size - 1)))
    except StopIteration:
        return


def iter_browse(model, *args, **kw):
    """
    Iterate and browse through record without filling the cache.
    `args` can be `cr, uid, ids` or just `ids` depending on kind of `model` (old/new api)
    """
    assert len(args) in [1, 3]  # either (cr, uid, ids) or (ids,)
    cr_uid = args[:-1]
    ids = args[-1]
    chunk_size = kw.pop("chunk_size", 200)  # keyword-only argument
    logger = kw.pop("logger", _logger)
    if kw:
        raise TypeError("Unknow arguments: %s" % ", ".join(kw))

    def browse(ids):
        model.invalidate_cache(*cr_uid)
        args = cr_uid + (list(ids),)
        return model.browse(*args)

    def end():
        model.invalidate_cache(*cr_uid)
        if 0:
            yield

    it = chain.from_iterable(chunks(ids, chunk_size, fmt=browse))
    if logger:
        it = log_progress(it, qualifier=model._name, logger=logger, size=len(ids))

    return chain(it, end())


def log_progress(it, qualifier="elements", logger=_logger, size=None):
    if size is None:
        size = len(it)
    size = float(size)
    t0 = t1 = datetime.datetime.now()
    for i, e in enumerate(it, 1):
        yield e
        t2 = datetime.datetime.now()
        if (t2 - t1).total_seconds() > 60:
            t1 = datetime.datetime.now()
            tdiff = t2 - t0
            logger.info(
                "[%.02f%%] %d/%d %s processed in %s (TOTAL estimated time: %s)",
                (i / size * 100.0),
                i,
                size,
                qualifier,
                tdiff,
                datetime.timedelta(seconds=tdiff.total_seconds() * size / i),
            )


class SelfPrint(object):
    """Class that will return a self representing string. Used to evaluate domains."""

    def __init__(self, name):
        self.__name = name

    def __getattr__(self, attr):
        return SelfPrint("%r.%s" % (self, attr))

    def __call__(self, *args, **kwargs):
        s = []
        for a in args:
            s.append(repr(a))
        for k, v in kwargs.items():
            s.append("%s=%r" % (k, v))
        return SelfPrint("%r(%s)" % (self, ", ".join(s)))

    def __add__(self, other):
        return SelfPrint("%r + %r" % (self, other))

    def __radd__(self, other):
        return SelfPrint("%r + %r" % (other, self))

    def __sub__(self, other):
        return SelfPrint("%r - %r" % (self, other))

    def __rsub__(self, other):
        return SelfPrint("%r - %r" % (other, self))

    def __mul__(self, other):
        return SelfPrint("%r * %r" % (self, other))

    def __rmul__(self, other):
        return SelfPrint("%r * %r" % (other, self))

    def __div__(self, other):
        return SelfPrint("%r / %r" % (self, other))

    def __rdiv__(self, other):
        return SelfPrint("%r / %r" % (other, self))

    def __floordiv__(self, other):
        return SelfPrint("%r // %r" % (self, other))

    def __rfloordiv__(self, other):
        return SelfPrint("%r // %r" % (other, self))

    def __mod__(self, other):
        return SelfPrint("%r % %r" % (self, other))

    def __rmod__(self, other):
        return SelfPrint("%r % %r" % (other, self))

    def __repr__(self):
        return self.__name

    __str__ = __repr__
