# Copyright 2022 ELITE Advanced technologies.
# Copyright 2022 ELITE - Salim ROUMILI
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

{
    "name": "Ela hr",
    "summary": """
        This module provides extension of hr functionalities.
        """,
    'category': 'Human Resources/Employees',
    'sequence': 199,
    "summary": "Extend employee information",
    "version": "15.0.0.0.0",
    "author": "ELITE Advanced technologies",
    "depends": [
        "base",
        "web",
        "calendar",
        "hr",
        "hr_recruitment",
        "hr_recruitment_survey",
        "project",
        "crm"
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/hr_applicant_view.xml",
        "views/hr_job_view.xml",
        "views/project_task_view.xml",
        "views/hr_applicant_tags_view.xml",
        "views/hr_applicant_objects_view.xml",
        "views/hr_recruitment_stage_view.xml",
        "views/project_task_type_view.xml",
        "views/calendar_event_view.xml",
        "views/web_calendar_templates.xml",
        "views/crm_lead_view.xml",
        "security/security.xml",
        "data/ir_cron.xml",
    ],
    "demo": [],
    "qweb": [
        #"views/web_calendar_templates.xml",
    ],
    "license": "AGPL-3",
    "installable": True,
    "application": True,
}
