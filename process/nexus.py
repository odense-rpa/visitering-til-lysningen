import datetime

from automation_server_client import WorkItemError
from kmd_nexus_client import NexusClientManager
from kmd_nexus_client.tree_helpers import filter_by_path

from process.forløb import forløb, forløb_til_indsats


def hent_opgaver(nexus: NexusClientManager, org_navn, beskrivelse_filter, navn_filter=None):
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


def hent_skema_og_bevillingsindsats(nexus: NexusClientManager, borger, cpr):
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
    bevillingsindsats_felt = next(
        (
            c
            for item in skema["items"]
            for c in item.get("children", [])
            if c.get("label") == "Bevillingsindsats"
        ),
        None,
    )
    bevillingsindsats = (
        bevillingsindsats_felt["value"]["name"]
        if bevillingsindsats_felt and bevillingsindsats_felt.get("value")
        else None
    )
    return skema, bevillingsindsats


def tilføj_borger_til_organisationer(nexus: NexusClientManager, borger, organisationskatalog):
    borgers_orgs = nexus.organisationer.hent_organisationer_for_borger(borger)
    for org_navn in organisationskatalog:
        if any(org.get("organization").get("name") == org_navn for org in borgers_orgs):
            continue
        org = nexus.organisationer.hent_organisation_ved_navn(org_navn)
        nexus.organisationer.tilføj_borger_til_organisation(borger, org)
    return nexus.organisationer.hent_organisationer_for_borger(borger)


def tilføj_indsatser(nexus: NexusClientManager, borger, skema, bevillingsindsats):
    pathway = nexus.borgere.hent_visning(borger)
    borgers_indsatsreferencer = nexus.borgere.hent_referencer(pathway)
    filtrerede = filter_by_path(
        borgers_indsatsreferencer,
        path_pattern="/Ældre og sundhedsfagligt grundforløb/*/Indsatser/*",
        active_pathways_only=False,
    )

    if not er_ÆL_indsats(bevillingsindsats):
        i_dag = datetime.date.today()
        opstarts_felter = {
            k: i_dag
            for k in (
                "workflowApprovedDate",
                "billingStartDate",
                "entryDate",
                "orderedDate",
                "workflowRequestedDate",
            )
        }
        for indsats_navn, check_navn in [
            (
                "Dag - opstartsindsatser SEL § 83 a",
                "Dag - Indtagelse af mad og drikke (tidsafgrænset rehabiliteringsforløb)",
            ),
            (
                "Aften - opstartsindsatser SEL § 83 a",
                "Aften - Indtagelse af mad og drikke (tidsafgrænset rehabiliteringsforløb)",
            ),
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
        (
            i
            for i in filtrerede
            if i["name"] == bevillingsindsats
            and i["workflowState"]["name"] == "Bevilliget"
        ),
        None,
    )
    if not allerede_bevilliget:
        skemanavn = skema["formDefinition"]["title"]
        relevant_forløb = next(
            (f for f in forløb_til_indsats if f["Skemanavn"] == skemanavn), None
        )
        if relevant_forløb:
            nu = datetime.datetime.now()
            felter = {
                "approvedDate": nu,
                "requestedDate": nu,
                "entryDate": nu,
                "requestedMovingDate": nu,
            }
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


def luk_afvist_opgave(nexus: NexusClientManager, aktivitets_id, borger):
    opgave = nexus.opgaver.hent_opgave_for_borger(borger, aktivitets_id)
    if opgave:
        nexus.opgaver.luk_opgave(opgave)


def tilføj_forløb(nexus: NexusClientManager, borger, skema):
    skemanavn = skema["formDefinition"]["title"]
    relevante_forløb = [f for f in forløb if f["Skemanavn"] == skemanavn]
    for f in relevante_forløb:
        nexus.forløb.opret_forløb(
            borger=borger,
            grundforløb_navn=f["Grundforløb"],
            forløb_navn=f["Forløb"],
        )


def opret_opgaver(
    nexus: NexusClientManager,
    skema,
    borgers_orgs,
    potentielle_organisationer,
    aktivitets_emne,
    borger_bevilliget,
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


def er_ÆL_indsats(bevillingsindsats):
    return bevillingsindsats in (
        "Midlertidigt ophold Lysningen - Borger fra hjemmet - ÆL § 17",
        "Midlertidigt ophold Lysningen - Borger fra sygehuset - ÆL § 17",
        "Midlertidigt ophold Lysningen - Borger aflastning pårørende - ÆL § 16",
    )
