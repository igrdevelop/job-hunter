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
    WORKINGNOMADS_ENABLED,
    JOBSPRESSO_ENABLED,
    BUILTIN_ENABLED,
    JUSTREMOTE_ENABLED,
    REMOTEOK_ENABLED,
    HIMALAYAS_ENABLED,
    FOURDAYWEEK_ENABLED,
    WEWORKREMOTELY_ENABLED,
    REMOTELEAF_ENABLED,
    ATS_AGGREGATOR_ENABLED,
    GMAIL_ENABLED,
    LINKEDIN_SCOUT_RELAY_ENABLED,
    TELEGRAM_CHANNELS_ENABLED,
)

# Registry — add new sources here as you build them
ALL_SOURCES = []

if JUSTJOIN_ENABLED:
    from hunter.sources.justjoin import JustJoinSource
    ALL_SOURCES.append(JustJoinSource())

if NOFLUFFJOBS_ENABLED:
    from hunter.sources.nofluffjobs import NoFluffJobsSource
    ALL_SOURCES.append(NoFluffJobsSource())

if LINKEDIN_ENABLED:
    from hunter.sources.linkedin import LinkedInSource
    ALL_SOURCES.append(LinkedInSource())

if BULLDOGJOB_ENABLED:
    from hunter.sources.bulldogjob import BulldogJobSource
    ALL_SOURCES.append(BulldogJobSource())

if PRACUJ_ENABLED:
    from hunter.sources.pracuj import PracujSource
    ALL_SOURCES.append(PracujSource())

if THEPROTOCOL_ENABLED:
    from hunter.sources.theprotocol import TheProtocolSource
    ALL_SOURCES.append(TheProtocolSource())

if SOLIDJOBS_ENABLED:
    from hunter.sources.solidjobs import SolidJobsSource
    ALL_SOURCES.append(SolidJobsSource())

if INHIRE_ENABLED:
    from hunter.sources.inhire import InhireSource
    ALL_SOURCES.append(InhireSource())

if JOBLEADS_ENABLED:
    from hunter.sources.jobleads import JobLeadsSource
    ALL_SOURCES.append(JobLeadsSource())

if ARBEITNOW_ENABLED:
    from hunter.sources.arbeitnow import ArbeitnowSource
    ALL_SOURCES.append(ArbeitnowSource())

if REMOTIVE_ENABLED:
    from hunter.sources.remotive import RemotiveSource
    ALL_SOURCES.append(RemotiveSource())

if WORKINGNOMADS_ENABLED:
    from hunter.sources.workingnomads import WorkingNomadsSource
    ALL_SOURCES.append(WorkingNomadsSource())

if JOBSPRESSO_ENABLED:
    from hunter.sources.jobspresso import JobspressoSource
    ALL_SOURCES.append(JobspressoSource())

if BUILTIN_ENABLED:
    from hunter.sources.builtin import BuiltInSource
    ALL_SOURCES.append(BuiltInSource())

if JUSTREMOTE_ENABLED:
    from hunter.sources.justremote import JustRemoteSource
    ALL_SOURCES.append(JustRemoteSource())

if REMOTEOK_ENABLED:
    from hunter.sources.remoteok import RemoteOkSource
    ALL_SOURCES.append(RemoteOkSource())

if HIMALAYAS_ENABLED:
    from hunter.sources.himalayas import HimalayasSource
    ALL_SOURCES.append(HimalayasSource())

if FOURDAYWEEK_ENABLED:
    from hunter.sources.fourdayweek import FourdayweekSource
    ALL_SOURCES.append(FourdayweekSource())

if WEWORKREMOTELY_ENABLED:
    from hunter.sources.weworkremotely import WeworkremotelySource
    ALL_SOURCES.append(WeworkremotelySource())

if REMOTELEAF_ENABLED:
    from hunter.sources.remoteleaf import RemoteleafSource
    ALL_SOURCES.append(RemoteleafSource())

if ATS_AGGREGATOR_ENABLED:
    from hunter.sources.ats_aggregator import AtsAggregatorSource
    ALL_SOURCES.append(AtsAggregatorSource())

if GMAIL_ENABLED:
    from hunter.sources.gmail import GmailSource
    ALL_SOURCES.append(GmailSource())

if LINKEDIN_SCOUT_RELAY_ENABLED:
    from hunter.sources.linkedin_scout_relay import LinkedInScoutRelaySource
    ALL_SOURCES.append(LinkedInScoutRelaySource())

if TELEGRAM_CHANNELS_ENABLED:
    from hunter.sources.telegram_channels import TelegramChannelsSource
    ALL_SOURCES.append(TelegramChannelsSource())


# ── Detail-page dispatch ─────────────────────────────────────────────────────

# Sources that handle detail-page fetching independent of the search-time
# ENABLED flags. apply_agent / expired_marker / gmail_enricher should still be
# able to fetch text from a URL even when its source is excluded from the hunt
# cycle. Build a fresh roster lazily so we don't pay the per-source import cost
# on cold paths.
_FETCH_ROSTER: list = []


def _fetch_roster() -> list:
    """Return every concrete source that knows how to claim/extract a URL.

    Unlike ALL_SOURCES this is independent of *_ENABLED config — even disabled
    sources own URL parsing for their domain.
    """
    global _FETCH_ROSTER
    if _FETCH_ROSTER:
        return _FETCH_ROSTER

    from hunter.sources.arbeitnow import ArbeitnowSource
    from hunter.sources.ats_aggregator import AtsAggregatorSource
    from hunter.sources.bulldogjob import BulldogJobSource
    from hunter.sources.fourdayweek import FourdayweekSource
    from hunter.sources.himalayas import HimalayasSource
    from hunter.sources.inhire import InhireSource
    from hunter.sources.jobleads import JobLeadsSource
    from hunter.sources.justjoin import JustJoinSource
    from hunter.sources.linkedin import LinkedInSource
    from hunter.sources.nofluffjobs import NoFluffJobsSource
    from hunter.sources.pracuj import PracujSource
    from hunter.sources.remoteleaf import RemoteleafSource
    from hunter.sources.remoteok import RemoteOkSource
    from hunter.sources.remotive import RemotiveSource
    from hunter.sources.solidjobs import SolidJobsSource
    from hunter.sources.theprotocol import TheProtocolSource
    from hunter.sources.weworkremotely import WeworkremotelySource
    from hunter.sources.workingnomads import WorkingNomadsSource
    from hunter.sources.jobspresso import JobspressoSource
    from hunter.sources.builtin import BuiltInSource
    from hunter.sources.justremote import JustRemoteSource
    from hunter.sources.linkedin_scout_relay import LinkedInScoutRelaySource
    from hunter.sources.telegram_channels import TelegramChannelsSource

    _FETCH_ROSTER = [
        JustJoinSource(),
        NoFluffJobsSource(),
        LinkedInSource(),
        BulldogJobSource(),
        PracujSource(),
        TheProtocolSource(),
        SolidJobsSource(),
        InhireSource(),
        JobLeadsSource(),
        ArbeitnowSource(),
        RemotiveSource(),
        WorkingNomadsSource(),
        JobspressoSource(),
        BuiltInSource(),
        JustRemoteSource(),
        RemoteOkSource(),
        HimalayasSource(),
        FourdayweekSource(),
        WeworkremotelySource(),
        RemoteleafSource(),
        AtsAggregatorSource(),
        LinkedInScoutRelaySource(),
        TelegramChannelsSource(),
    ]
    return _FETCH_ROSTER


def fetch_job_text(url: str) -> str:
    """Fetch and return plain-text job posting for a URL.

    Resolves the URL to the first source whose ``matches_url`` returns True and
    delegates to ``source.fetch_text``. Falls back to the generic HTML extractor
    when nothing matches. Tracking/UTM params are stripped before dispatch.
    """
    from hunter.sources.html_fallback import clean_url, fetch_html

    cleaned = clean_url(url)
    for src in _fetch_roster():
        if src.matches_url(cleaned):
            return src.fetch_text(cleaned)
    return fetch_html(cleaned)
