from hunter.config import (
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
)
from hunter.sources.justjoin import JustJoinSource
from hunter.sources.nofluffjobs import NoFluffJobsSource

# Registry — add new sources here as you build them
ALL_SOURCES = [
    JustJoinSource(),
    NoFluffJobsSource(),
]

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

if REMOTEOK_ENABLED:
    from hunter.sources.remoteok import RemoteOkSource
    ALL_SOURCES.append(RemoteOkSource())

if HIMALAYAS_ENABLED:
    from hunter.sources.himalayas import HimalayasSource
    ALL_SOURCES.append(HimalayasSource())
