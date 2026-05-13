import asyncio
import datetime
import logging
import sys
import argparse
import os

from automation_server_client import (
    AutomationServer,
    Workqueue,
    WorkItemError,
    Credential,
    WorkItemStatus,
)

from odk_tools.tracking import Tracker
from process.config import load_excel_mapping, get_excel_mapping
from kmd_nexus_client import NexusClientManager

tracker: Tracker = None
nexus: NexusClientManager = None
potentielle_organisationer: list = []
proces_navn = "Visitering til Lysningen"


def _hent_opgaver(org_navn, beskrivelse_filter, navn_filter=None):
    org = nexus.organisationer.hent_organisation_ved_navn(org_navn)
    aktivitetsliste = nexus.aktivitetslister.hent_aktivitetsliste(
        navn="Opgaver - 7 dage bagud og 7 dage frem",
        organisation=org,
        medarbejder=None,
    )
    return [
        o
        for o in aktivitetsliste
        if beskrivelse_filter in o.get("description", "")
        and (navn_filter is None or navn_filter in o.get("name", ""))
    ]


async def populate_queue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from populate workqueue!")

    opgaver = _hent_opgaver(
        "Lysningen indflytning",
        "Lysningen indflytning, Robotbruger Odense",
        "BEVILGET - Midlertidigt ophold på Lysningen",
    )
    opgaver_afvist = _hent_opgaver(
        "Robot afvist Lysningen",
        "Robot afvist Lysningen, Robotbruger Odense",
    )

    for opgave in opgaver + opgaver_afvist:
        cpr = opgave["patients"][0]["patientIdentifier"]["identifier"]
        workqueue.get_item_by_reference(cpr)
        # TODO: find ud hvordan vi tjekker om CPR er i køen indenfor 2 dage med bevilget - hvis ja så skip
        data = {
            "aktivitets_id": opgave["id"],
            "cpr": cpr,
            "aktivitets_emne": opgave["name"],
            "aktivitets_besked": opgave["description"],
            "aktivitets_status": opgave["status"],
            "aktivitets_dato": opgave["creationDate"],
        }
        workqueue.add_item(data=data, reference=cpr)


async def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from process workqueue!")
    organisationskatalog = [
        "Økonomi og Effekt, midlertidige pladser",
        "Lysningen",
        "Myndighed Lysningen"
    ]

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
            bevillingsindsats = ""

            try:
                borger = nexus.borgere.hent_borger(data["cpr"])
                skemareferencer = nexus.skemaer.hent_skemareferencer(borger=borger)
                skemareferencer = [
                    s
                    for s in skemareferencer
                    if s.get("Navn")
                    == "Indstilling og afgørelse til midlertidigt ophold på Lysningen - Servicelov"
                    or s.get("Navn")
                    == "Indstilling og afgørelse til midlertidigt ophold på Lysningen - Ældrelov"
                ]
                if not skemareferencer:
                    raise WorkItemError(
                        f"Borger med CPR {data['cpr']} har ingen relevante skemareferencer"
                    )
                # Finder det nyeste skemareference baseret på "Dato" feltet
                skemareference = max(skemareferencer, key=lambda s: s["Dato"])
                skema = nexus.skemaer.hent_skema_fra_reference(skemareference)
                bevillingsindsats = next(
                    c for item in skema["items"] for c in item.get("children", [])
                    if c.get("label") == "Bevillingsindsats"
                )["value"]["name"]

                # Tilføjer borger til 3 bestemt orgs.
                borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(borger)
                for org_navn in organisationskatalog:
                    if any(org.get("organization").get("name") == org_navn for org in borgers_orgs):
                        continue
                    org = nexus.organisationer.hent_organisation_ved_navn(org_navn)
                    nexus.organisationer.tilføj_borger_til_organisation(borger, org)

                borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(borger)

                # finder et match mellem borgers orgs og potentielle orgs og tjekker om der allerede er en opgave for borger i den organisation med samme startdato som i dag, hvis ikke så oprettes en opgave for hver matchende org
                borgers_opgave = nexus.opgaver.hent_opgaver(skema)
                matchende_orgs = [
                    org
                    for org in borgers_orgs
                    if org.get("organization", {}).get("name") in potentielle_organisationer
                ]
                for org in matchende_orgs:
                    if any(
                        opgave
                        for opgave in borgers_opgave
                        if org.get("organization", {}).get("name") in opgave.get("organizationAssignee", {}).get("displayName", "")
                        and 
                        opgave["startDate"] == datetime.date.today().isoformat()
                    ):
                        continue
                    nexus.opgaver.opret_opgave(
                        objekt=skema,
                        opgave_type=data["aktivitets_emne"],
                        titel=data["aktivitets_emne"],
                        ansvarlig_organisation=org["organization"]["name"],
                        start_dato=datetime.date.today(),
                        forfald_dato=datetime.date.today(),
                        beskrivelse=data["aktivitets_emne"],
                    )
                    print("hej")




                print(f"Borger med CPR {data['cpr']} har {len(skemareferencer)} relevante skemareferencer, bruger nyeste fra {skemareference['Dato']}")

            except WorkItemError as e:
                # A WorkItemError represents a soft error that indicates the item should be passed to manual processing or a business logic fault
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    # Initialize external systems for automation here..
    tracking_credential = Credential.get_credential("Odense SQL Server")
    roboa_credential = Credential.get_credential("RoboA")  # bruges til at hente emails
    nexus_credential = Credential.get_credential("KMD Nexus - produktion")

    tracker = Tracker(
        username=tracking_credential.username, password=tracking_credential.password
    )

    nexus = NexusClientManager(
        client_id=nexus_credential.username,
        client_secret=nexus_credential.password,
        instance=nexus_credential.data["instance"],
        timeout=60,
    )

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=proces_navn)
    parser.add_argument(
        "--excel-file",
        default=os.environ.get("EXCEL_MAPPING_PATH"),
        help="Path to the Excel file containing mapping data (default: ./Regelsæt.xlsx)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Populate the queue with test data and exit",
    )
    args = parser.parse_args()

    # Validate Excel files exists (skip validation for Windows paths on Linux)
    def is_windows_path(path: str) -> bool:
        """Check if path is a Windows path (has drive letter or UNC path)"""
        return (
            (len(path) > 1 and path[1] == ":")
            or path.startswith("\\\\")
            or path.startswith("//")
        )

    # Load excel mapping data once on startup (only if files exist on current system)
    if os.path.isfile(args.excel_file):
        load_excel_mapping(args.excel_file)
    elif not is_windows_path(args.excel_file):
        raise FileNotFoundError(f"Excel file not found: {args.excel_file}")

    potentielle_organisationer = [
        row["Organisationer som muligvis skal have besked ved visitering til Lysningen"]
        for row in get_excel_mapping().get("Liste", [])
    ]
    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue(WorkItemStatus.NEW)
        asyncio.run(populate_queue(workqueue))
        exit(0)

    # Process workqueue
    asyncio.run(process_workqueue(workqueue))
