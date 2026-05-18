import logging

from hunter.config import (
    JUSTJOIN_ENABLED,
    NOFLUFFJOBS_ENABLED,
    LINKEDIN_ENABLED,
    BULLDOGJOB_ENABLED,
    PRACUJ_ENABLED,
    THEPROTOCOL_ENABLED,
    SOLIDJOBS_ENABLED,
    INHIRE_ENABLED,
    JOBLEADS_ENABLED,
    ARBEITNOW_ENABLED,
    REMOTIVE_ENABLED,
    REMOTEOK_ENABLED,
    HIMALAYAS_ENABLED,
    FOURDAYWEEK_ENABLED,
    WEWORKREMOTELY_ENABLED,
    REMOTELEAF_ENABLED,
    ATS_AGGREGATOR_ENABLED,
    GMAIL_ENABLED,
)

_log = logging.getLogger(__name__)

ALL_SOURCES: list = []


def _try_add(flag: bool, module: str, cls_name: str) -> None:
    if not flag:
        return
    try:
        mod = __import__(module, fromlist=[cls_name])
        ALL_SOURCES.append(getattr(mod, cls_name)())
    except Exception as exc:
        _log.warning("%s disabled — import error: %s", cls_name, exc)


_try_add(JUSTJOIN_ENABLED,       "hunter.sources.justjoin",       "JustJoinSource")
_try_add(NOFLUFFJOBS_ENABLED,    "hunter.sources.nofluffjobs",    "NoFluffJobsSource")
_try_add(LINKEDIN_ENABLED,       "hunter.sources.linkedin",       "LinkedInSource")
_try_add(BULLDOGJOB_ENABLED,     "hunter.sources.bulldogjob",     "BulldogJobSource")
_try_add(PRACUJ_ENABLED,         "hunter.sources.pracuj",         "PracujSource")
_try_add(THEPROTOCOL_ENABLED,    "hunter.sources.theprotocol",    "TheProtocolSource")
_try_add(SOLIDJOBS_ENABLED,      "hunter.sources.solidjobs",      "SolidJobsSource")
_try_add(INHIRE_ENABLED,         "hunter.sources.inhire",         "InhireSource")
_try_add(JOBLEADS_ENABLED,       "hunter.sources.jobleads",       "JobLeadsSource")
_try_add(ARBEITNOW_ENABLED,      "hunter.sources.arbeitnow",      "ArbeitnowSource")
_try_add(REMOTIVE_ENABLED,       "hunter.sources.remotive",       "RemotiveSource")
_try_add(REMOTEOK_ENABLED,       "hunter.sources.remoteok",       "RemoteOkSource")
_try_add(HIMALAYAS_ENABLED,      "hunter.sources.himalayas",      "HimalayasSource")
_try_add(FOURDAYWEEK_ENABLED,    "hunter.sources.fourdayweek",    "FourdayweekSource")
_try_add(WEWORKREMOTELY_ENABLED, "hunter.sources.weworkremotely", "WeworkremotelySource")
_try_add(REMOTELEAF_ENABLED,     "hunter.sources.remoteleaf",     "RemoteleafSource")
_try_add(ATS_AGGREGATOR_ENABLED, "hunter.sources.ats_aggregator", "AtsAggregatorSource")
_try_add(GMAIL_ENABLED,          "hunter.sources.gmail",          "GmailSource")
