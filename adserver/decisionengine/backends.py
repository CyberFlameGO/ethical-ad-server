"""Ad decision backends."""
import logging
import random

from django.db import models
from user_agents import parse

from ..constants import AFFILIATE_CAMPAIGN
from ..constants import ALL_CAMPAIGN_TYPES
from ..constants import COMMUNITY_CAMPAIGN
from ..constants import HOUSE_CAMPAIGN
from ..constants import PAID_CAMPAIGN
from ..models import Flight
from ..utils import get_ad_day
from ..utils import get_client_user_agent

log = logging.getLogger(__name__)


class BaseAdDecisionBackend:

    """A base decision backend -- other decision backends should extend this."""

    def __init__(self, request, placements, publisher, **kwargs):
        """
        Initialize an ad decision based on the request data.

        :param request: the HttpRequest object with geo data attached from GeolocationMiddleware
        :param placements: possible positions for the ad to go
        :param kwargs: Any additional possible arguments for the backend
        """
        self.request = request
        self.user_agent = parse(get_client_user_agent(request))
        self.placements = placements
        self.publisher = publisher

        self.ad_types = [p["ad_type"] for p in self.placements]

        self.geolocation = request.geo

        # Optional parameters
        self.keywords = kwargs.get("keywords", []) or []
        requested_campaign_types = kwargs.get("campaign_types", []) or []
        if not requested_campaign_types:
            requested_campaign_types = ALL_CAMPAIGN_TYPES

        # Add default keywords from publisher
        if self.publisher.keywords:
            log.debug(
                "Adding default keywords: publisher=%s keywords=%s",
                self.publisher.slug,
                self.publisher.keywords,
            )
            merged_keywords = set(self.keywords) | set(self.publisher.keywords)
            self.keywords = list(merged_keywords)

        # Publishers can request certain campaign types
        # But only if those types are allowed by database settings
        self.campaign_types = []
        if (
            self.publisher.allow_paid_campaigns
            and PAID_CAMPAIGN in requested_campaign_types
        ):
            self.campaign_types.append(PAID_CAMPAIGN)
        if (
            self.publisher.allow_affiliate_campaigns
            and AFFILIATE_CAMPAIGN in requested_campaign_types
        ):
            self.campaign_types.append(AFFILIATE_CAMPAIGN)
        if (
            self.publisher.allow_community_campaigns
            and COMMUNITY_CAMPAIGN in requested_campaign_types
        ):
            self.campaign_types.append(COMMUNITY_CAMPAIGN)
        if (
            self.publisher.allow_house_campaigns
            and HOUSE_CAMPAIGN in requested_campaign_types
        ):
            self.campaign_types.append(HOUSE_CAMPAIGN)

        self.url = kwargs.get("url") or ""

        # When set, only return a specific ad or ads from a campaign
        self.ad_slug = kwargs.get("ad_slug")
        self.campaign_slug = kwargs.get("campaign_slug")

    def get_ad_and_placement(self):
        """
        Choose an ad to display.

        This is the main entry point for making an ad decision. It makes the decision
        based on the passed information including the request, the available places
        for an ad (placements), and any other data passed to the backend.

        ::

            backend = BaseAdDecisionBackendSubclass(request, placements)
            ad, placement = backend.get_ad_and_placement()

            if ad and placement
                # ...  # there could be no matching ads

        :return: A 2-tuple of the `Advertisement` object and the matching `placement`
        """
        raise NotImplementedError(
            "subclasses of BaseAdDecisionBackend must override get_ad_and_placement()"
        )

    def get_placement(self, advertisement):
        """Gets the first matching placement for a given ad."""
        if not advertisement:
            # Always select the placement if there is only 1 for Decisions
            if len(self.placements) == 1:
                return self.placements[0]
            return None

        for placement in self.placements:
            # A placement "matches" if the ad type matches
            # If the ad or campaign is specified, they must also match
            if (
                placement["ad_type"] in [t.slug for t in advertisement.ad_types.all()]
                and (not self.ad_slug or advertisement.slug == self.ad_slug)
                and (
                    not self.campaign_slug
                    or advertisement.flight.campaign.slug == self.campaign_slug
                )
            ):
                return placement

        return None

    def should_display_ads(self):
        """Whether to not display ads based on the user, request, or other settings."""
        return True


class AdvertisingDisabledBackend(BaseAdDecisionBackend):

    """A backend where no ads are displayed."""

    def get_ad_and_placement(self):
        return None, None

    def should_display_ads(self):
        return False


class AdvertisingEnabledBackend(BaseAdDecisionBackend):

    """A backend where ads are displayed (default ad order)."""

    def get_candidate_flights(self):
        """
        Queries for all valid, live flights.

        This does not take into account any priority among the ads or any clicks required.
        """
        if not self.should_display_ads():
            return Flight.objects.none()

        flights = (
            Flight.objects.filter(
                advertisements__ad_types__slug__in=self.ad_types,
                campaign__campaign_type__in=self.campaign_types,
            )
            .filter(
                # Deprecated: remove after publisher groups are rolled out and configured in production
                # At that point, only filter by publisher groups
                models.Q(campaign__publishers=self.publisher)
                | models.Q(
                    campaign__publisher_groups__in=self.publisher.publisher_groups.all()
                )
            )
            .exclude(campaign__exclude_publishers=self.publisher)
        )

        if self.campaign_types != ALL_CAMPAIGN_TYPES:
            log.debug(
                "Ads restricted to the following campaign types: %s",
                self.campaign_types,
            )

        # Specifying the ad or campaign slug skips filtering by live or date
        if self.ad_slug:
            log.debug("Restricting ad decision ad_slug=%s", self.ad_slug)
            flights = flights.filter(advertisements__slug=self.ad_slug)
        elif self.campaign_slug:
            log.debug("Restricting ad decision campaign=%s", self.campaign_slug)
            flights = flights.filter(campaign__slug=self.campaign_slug)
        else:
            flights = flights.filter(live=True, start_date__lte=get_ad_day().date())

            # Ensure there's a live ad of the chosen types for each flight
            flights = flights.annotate(
                num_ads=models.Count(
                    "advertisements",
                    filter=models.Q(
                        advertisements__ad_types__slug__in=self.ad_types,
                        advertisements__live=True,
                    ),
                )
            ).filter(num_ads__gt=0)

        # Ensure we prefetch necessary data so it doesn't result in N queries for each flight
        return flights.select_related("campaign")

    def filter_flight(self, flight):
        """
        Apply flight targeting.

        * $/clicks/views left on the campaign
        * flight clicks needed today to keep on pace
        * geo and keyword filters
        """
        if self.campaign_slug or self.ad_slug:
            # Skip filtering if the ad or campaign are specified
            return True

        # Skip if we aren't meant to show to this country/state/dma
        if not flight.show_to_geo(self.geolocation):
            return False

        # Skip if we aren't meant to show to these keywords
        if not flight.show_to_keywords(self.keywords):
            return False

        # Skip if we aren't meant to show to this traffic because it is mobile or non-mobile
        if not flight.show_to_mobile(self.user_agent.is_mobile):
            return False

        # Skip if there are no clicks or views needed today (ad pacing)
        if flight.weighted_clicks_needed_today() <= 0:
            return False

        return True

    def select_flight(self):
        """Naively select a flight from the candidates."""
        flights = self.get_candidate_flights()

        # Apply targeting
        flights = [flight for flight in flights if self.filter_flight(flight)]

        if flights:
            return random.choice(flights)

        return None

    def select_ad_for_flight(self, flight):
        """Naively choose an ad from the selected flight."""
        if not flight:
            return None

        return (
            flight.advertisements.filter(live=True, ad_types__slug__in=self.ad_types)
            .order_by("?")
            .first()
        )

    def get_ad_and_placement(self):
        flight = self.select_flight()
        ad = self.select_ad_for_flight(flight)
        return ad, self.get_placement(ad)


class ProbabilisticFlightBackend(AdvertisingEnabledBackend):

    """
    A backend where flights are selected randomly weighted by the clicks needed today.

    * Randomly select a paid ad based with random weights based on clicks needed
    * If no matching paid ads, randomly select a community ad
    * If no matching community ad, randomly select a house ad
    """

    def select_flight(self):
        """
        Select a flight from the candidates.

        * Choose paid over community over house campaigns
        * Prioritize the flight that needs the most impressions
        """
        flights = self.get_candidate_flights()

        paid_flights = []
        affiliate_flights = []
        community_flights = []
        house_flights = []

        for flight in flights:
            if flight.campaign.campaign_type == PAID_CAMPAIGN:
                paid_flights.append(flight)
            elif flight.campaign.campaign_type == AFFILIATE_CAMPAIGN:
                affiliate_flights.append(flight)
            elif flight.campaign.campaign_type == COMMUNITY_CAMPAIGN:
                community_flights.append(flight)
            else:
                house_flights.append(flight)

        if flights and (self.ad_slug or self.campaign_slug):
            # Ignore priorities for forcing a specific ad/campaign
            return random.choice(flights)

        for possible_flights in (
            paid_flights,
            affiliate_flights,
            community_flights,
            house_flights,
        ):
            # Choose a flight based on the impressions needed
            flight_range = []
            total_clicks_needed = 0
            for flight in possible_flights:
                if not self.filter_flight(flight):
                    continue

                # If any impressions/clicks are needed, add this flight
                # to the possible list of flights
                if any(
                    (
                        (flight.clicks_needed_today() > 0),
                        (flight.views_needed_today() > 0),
                    )
                ):
                    # NOTE: takes into account views for CPM ads
                    # Takes eCPM (CTR * CPC for CPC ads) into account
                    weighted_clicks_needed_today = flight.weighted_clicks_needed_today(
                        self.publisher
                    )

                    flight_range.append(
                        [
                            total_clicks_needed,
                            total_clicks_needed + weighted_clicks_needed_today,
                            flight,
                        ]
                    )
                    total_clicks_needed += weighted_clicks_needed_today

            choice = random.randint(0, total_clicks_needed)
            for min_clicks, max_clicks, flight in flight_range:
                if min_clicks <= choice <= max_clicks:
                    return flight

        return None

    def select_ad_for_flight(self, flight):
        """
        Choose an ad from the selected flight.

        Apply weighting to the ad based on the requested placement priority.
        """
        if not flight:
            return None

        chosen_ad = None
        max_priority = 10
        weighted_ad_choices = []

        if self.ad_slug:
            # Ignore live and adtype checks when forcing a specific ad
            candidate_ads = flight.advertisements.filter(slug=self.ad_slug)
        else:
            candidate_ads = flight.advertisements.filter(
                live=True, ad_types__slug__in=self.ad_types
            )

        candidate_ads = candidate_ads.select_related("flight").prefetch_related(
            "ad_types"
        )

        for advertisement in candidate_ads:
            placement = self.get_placement(advertisement)
            if not placement:
                log.warning(
                    "Couldn't find a matching ad placement. ad=%s, placements=%s",
                    advertisement,
                    self.placements,
                )
                continue
            priority = placement.get("priority", 1)
            for _ in range(max_priority + 1 - priority):
                weighted_ad_choices.append(advertisement)

        if weighted_ad_choices:
            chosen_ad = random.choice(weighted_ad_choices)
        else:
            log.warning(
                "Chosen flight has no matching live ads! flight=%s, ad_types=%s",
                flight,
                self.ad_types,
            )

        return chosen_ad
