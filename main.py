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
from process.forløb import forløb, forløb_til_indsats
from kmd_nexus_client import NexusClientManager
from kmd_nexus_client.tree_helpers import filter_by_path

tracker: Tracker = None
nexus: NexusClientManager = None
potentielle_organisationer: list = []
proces_navn = "Visitering til Lysningen"


def _opret_opgaver(
    skema, borgers_orgs, potentielle_organisationer, aktivitets_emne, borger_bevilliget
):
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
            if org.get("organization", {}).get("name")
            == opgave.get("organizationAssignee", {}).get("displayName", "")
            and opgave["startDate"] == datetime.date.today().isoformat()
        ):
            continue
        opgave = nexus.opgaver.opret_opgave(
            objekt=skema,
            opgave_type=aktivitets_emne,
            titel=aktivitets_emne,
            ansvarlig_organisation=org["organization"]["name"],
            start_dato=datetime.date.today(),
            forfald_dato=datetime.date.today(),
            beskrivelse=aktivitets_emne,
        )
        if not opgave:
            raise WorkItemError(
                f"Kunne ikke oprette opgave i organisation {org['organization']['name']}"
            )
    if borger_bevilliget:
        opgave = nexus.opgaver.opret_opgave(
            objekt=skema,
            opgave_type=aktivitets_emne,
            titel=aktivitets_emne,
            ansvarlig_organisation="Myndighed Sygeplejerådgivere",
            start_dato=datetime.date.today(),
            forfald_dato=datetime.date.today(),
            beskrivelse=aktivitets_emne,
        )
        if not opgave:
            raise WorkItemError(
                "Kunne ikke oprette opgave i organisation Myndighed Sygeplejerådgivere"
            )


def _er_ÆL_indsats(bevillingsindsats):
    return bevillingsindsats in (
        "Midlertidigt ophold Lysningen - Borger fra hjemmet - ÆL § 17",
        "Midlertidigt ophold Lysningen - Borger fra sygehuset - ÆL § 17",
        "Midlertidigt ophold Lysningen - Borger aflastning pårørende - ÆL § 16",
    )


def _tilføj_forløb(borger, skema):
    skemanavn = skema["formDefinition"]["title"]
    relevante_forløb = [f for f in forløb if f["Skemanavn"] == skemanavn]
    for f in relevante_forløb:
        nexus.forløb.opret_forløb(
            borger=borger,
            grundforløb_navn=f["Grundforløb"],
            forløb_navn=f["Forløb"],
        )


def _tilføj_borger_til_organisationer(borger, organisationskatalog):
    borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(borger)
    for org_navn in organisationskatalog:
        if any(org.get("organization").get("name") == org_navn for org in borgers_orgs):
            continue
        org = nexus.organisationer.hent_organisation_ved_navn(org_navn)
        nexus.organisationer.tilføj_borger_til_organisation(borger, org)
    return nexus.organisationer.hent_organisationer_for_borger(borger)


def _hent_skema_og_bevillingsindsats(borger, cpr):
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
            f"Borger med CPR {cpr[-4:]} har ingen relevante skemareferencer"
        )
    skemareference = max(skemareferencer, key=lambda s: s["Dato"])
    skema = nexus.skemaer.hent_skema_fra_reference(skemareference)
    bevillingsindsats = next(
        c
        for item in skema["items"]
        for c in item.get("children", [])
        if c.get("label") == "Bevillingsindsats"
    )["value"]["name"]
    return skema, bevillingsindsats


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


def _tilføj_indsatser(borger, skema, bevillingsindsats):
    pathway = nexus.borgere.hent_visning(borger)
    borgers_indsatsreferencer = nexus.borgere.hent_referencer(pathway)
    filtrerede = filter_by_path(
        borgers_indsatsreferencer,
        path_pattern="/Ældre og sundhedsfagligt grundforløb/*/Indsatser/*",
        active_pathways_only=False,
    )

    if not _er_ÆL_indsats(bevillingsindsats):
        i_dag = datetime.date.today()
        opstarts_felter = {k: i_dag for k in ("workflowApprovedDate", "billingStartDate", "entryDate", "orderedDate", "workflowRequestedDate")}
        for indsats_navn, check_navn in [
            ("Dag - opstartsindsatser SEL § 83 a",   "Dag - Indtagelse af mad og drikke (tidsafgrænset rehabiliteringsforløb)"),
            ("Aften - opstartsindsatser SEL § 83 a", "Aften - Indtagelse af mad og drikke (tidsafgrænset rehabiliteringsforløb)"),
        ]:
            if not any(i for i in filtrerede if i["name"] == check_navn):
                nexus.indsatser.opret_indsats(
                    borger=borger,
                    grundforløb="Ældre og sundhedsfagligt grundforløb",
                    forløb="Sag SOFF: Pleje, omsorg og træning",
                    felter=opstarts_felter,
                    leverandør="Lysningen",
                    oprettelsesform="Ansøg, Bevilg, Bestil",
                    indsats=indsats_navn,
                )

    allerede_bevilliget = next(
        (i for i in filtrerede if i["name"] == bevillingsindsats and i["workflowState"]["name"] == "Bevilliget"),
        None,
    )
    if not allerede_bevilliget:
        skemanavn = skema["formDefinition"]["title"]
        relevant_forløb = next((f for f in forløb_til_indsats if f["Skemanavn"] == skemanavn), None)
        if relevant_forløb:
            nu = datetime.datetime.now()
            felter = {"approvedDate": nu, "requestedDate": nu, "entryDate": nu, "requestedMovingDate": nu}
            if bevillingsindsats in (
                "Midlertidigt ophold Lysningen - Borger aflastning pårørende",
                "Midlertidigt ophold Lysningen - Borger aflastning pårørende - ÆL § 16",
            ):
                felter |= {"waitingListType": "NOT_RELEVANT", "priority": "ACUTE"}
            nexus.indsatser.opret_indsats(
                borger=borger,
                grundforløb="Ældre og sundhedsfagligt grundforløb",
                forløb=relevant_forløb["Forløb"],
                indsats=bevillingsindsats,
                felter=felter,
                leverandør="",
                oprettelsesform="Ansøg, Bevilg",
            )


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
        "Myndighed Lysningen",
    ]

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
            borger_bevilliget = True
            if data["aktivitets_emne"] != "BEVILGET - Midlertidigt ophold på Lysningen":
                borger_bevilliget = False
            bevillingsindsats = ""

            try:
                borger = nexus.borgere.hent_borger(data["cpr"])
                skema, bevillingsindsats = _hent_skema_og_bevillingsindsats(
                    borger, data["cpr"]
                )

                # Tilføjer borger til 3 bestemt orgs.
                if borger_bevilliget:
                    borgers_orgs = _tilføj_borger_til_organisationer(
                        borger, organisationskatalog
                    )
                else:
                    borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(
                        borger
                    )

                # finder et match mellem borgers orgs og potentielle orgs og tjekker om der allerede er en opgave for borger i den organisation med samme startdato som i dag, hvis ikke så oprettes en opgave for hver matchende org
                # _opret_opgaver(
                #     skema,
                #     borgers_orgs,
                #     potentielle_organisationer,
                #     data["aktivitets_emne"],
                #     borger_bevilliget,
                # )

                # tilføjer forløb til borger
                # if borger_bevilliget:
                #     _tilføj_forløb(borger, skema)

                # tilføjer indsatser til borger
                if borger_bevilliget:
                    _tilføj_indsatser(borger, skema, bevillingsindsats)

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
