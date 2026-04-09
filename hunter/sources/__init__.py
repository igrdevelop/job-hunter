from hunter.config import LINKEDIN_ENABLED, BULLDOGJOB_ENABLED, PRACUJ_ENABLED
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
