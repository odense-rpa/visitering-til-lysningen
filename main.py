import asyncio
import logging
import sys
import argparse
import os
from datetime import datetime, timedelta

from automation_server_client import (
    AutomationServer,
    Workqueue,
    WorkItemError,
    Credential,
    WorkItemStatus,
)

from odk_tools.tracking import Tracker
from process.config import load_excel_mapping, get_excel_mapping
from process.nexus import (
    hent_opgaver,
    hent_skema_og_bevillingsindsats,
    tilføj_borger_til_organisationer,
    tilføj_indsatser,
    luk_afvist_opgave,
    opret_opgaver,
    tilføj_forløb,
)
from kmd_nexus_client import NexusClientManager

tracker: Tracker = None
nexus: NexusClientManager = None
potentielle_organisationer: list = []
proces_navn = "Visitering til Lysningen"


async def populate_queue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from populate workqueue!")

    opgaver = hent_opgaver(
        nexus,
        "Lysningen indflytning",
        "Lysningen indflytning, Robotbruger Odense",
        "BEVILGET - Midlertidigt ophold på Lysningen",
    )
    opgaver_afvist = hent_opgaver(
        nexus,
        "Robot afvist Lysningen",
        "Robot afvist Lysningen, Robotbruger Odense",
    )

    for opgave in opgaver + opgaver_afvist:
        cpr = opgave["patients"][0]["patientIdentifier"]["identifier"]
        existing_items = workqueue.get_item_by_reference(cpr, status=WorkItemStatus.COMPLETED)
        bevilget_emne = "BEVILGET - Midlertidigt ophold på Lysningen"
        cutoff = datetime.now() - timedelta(days=2)
        if any(
            item.data.get("aktivitets_emne") == bevilget_emne and item.created_at >= cutoff
            for item in existing_items
        ):
            logger.info(f"Skipping CPR {cpr}: already in queue with bevilget within 2 days")
            continue
        data = {
            "aktivitets_id": opgave["id"],
            "cpr": cpr,
            "aktivitets_emne": opgave["name"],
            "aktivitets_dato": opgave["creationDate"],
        }
        workqueue.add_item(data=data, reference=cpr)


async def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from process workqueue!")
    organisationskatalog = [
        "Økonomi og Effekt, midlertidige pladser",
        "Lysningen",
        "Myndighed Lysningen",
    ]

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
            borger_bevilliget = (
                data["aktivitets_emne"] == "BEVILGET - Midlertidigt ophold på Lysningen"
            )

            try:
                borger = nexus.borgere.hent_borger(data["cpr"])
                skema, bevillingsindsats = hent_skema_og_bevillingsindsats(
                    nexus, borger, data["cpr"]
                )

                # Tilføjer borger til 3 bestemt orgs.
                if borger_bevilliget:
                    borgers_orgs = tilføj_borger_til_organisationer(
                        nexus, borger, organisationskatalog
                    )
                else:
                    borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(
                        borger
                    )

                # finder et match mellem borgers orgs og potentielle orgs og tjekker om der allerede er en opgave for borger i den organisation med samme startdato som i dag, hvis ikke så oprettes en opgave for hver matchende org
                opret_opgaver(
                    nexus,
                    skema,
                    borgers_orgs,
                    potentielle_organisationer,
                    data["aktivitets_emne"],
                    borger_bevilliget,
                )

                # tilføjer forløb til borger
                if borger_bevilliget:
                    tilføj_forløb(nexus, borger, skema)

                # tilføjer indsatser til borger
                if borger_bevilliget:
                    tilføj_indsatser(nexus, borger, skema, bevillingsindsats)

                if not borger_bevilliget:
                    luk_afvist_opgave(nexus, data["aktivitets_id"], borger)

                tracker.track_task(process_name=proces_navn)

            except WorkItemError as e:
                # A WorkItemError represents a soft error that indicates the item should be passed to manual processing or a business logic fault
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    # Initialize external systems for automation here..
    tracking_credential = Credential.get_credential("Odense SQL Server")
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
