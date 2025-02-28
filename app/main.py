from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.routing import APIRoute
from json2xml import json2xml
from json2xml.utils import readfromstring
from lxml import etree
from lxml.builder import E
from typing import Callable, List, Optional
import csv
import os
import re
import requests

load_dotenv()


class StripSpineOMaticAPIKey(APIRoute):
    def get_route_handler(self) -> Callable:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            # Srip apikey URL param hardcoded in SpineOMatic before passing to FOLIO
            # -- appended as '&apikey='
            clean_barcode = request.path_params["barcode"].partition("&")[0]
            request.path_params["barcode"] = clean_barcode
            response: Response = await original_route_handler(request)
            return response

        return custom_route_handler


app = FastAPI()
router = APIRouter(route_class=StripSpineOMaticAPIKey)


@app.get("/")
async def read_root():
    return {"Hello": "World"}


@router.get("/items/{barcode}")
async def read_item(
    barcode: int,
    format: Optional[str] = "xml",
    replace: Optional[bool] = True,
    transform: Optional[bool] = True,
):
    url = f"{os.getenv('OKAPI_URL')}/inventory/items"
    params = {"query": f"(barcode=={barcode})"}
    headers = {
        "Content-Type": "application/json",
        "X-Okapi-Tenant": os.getenv("OKAPI_TENANT"),
        "X-Okapi-Token": _okapi_login(),
    }
    folio_inventory = requests.get(url, params=params, headers=headers)
    plate_funds = ["p2053", "p2052"]

    if format == "json":
        return folio_inventory.json()
    else:
        data = readfromstring(folio_inventory.text)
        # FOLIO /inventory/items endpoint always returns list
        # -- trim to single item because SpineOMatic expects object as root node
        try:
            item = data["items"][0]

            holdings = item["holdingsRecordId"]
            pol = requests.get(
                os.getenv("OKAPI_URL") + "/orders/holding-summary/" + holdings,
                headers=headers,
            ).json()
            if len(pol["holdingSummaries"]) > 0:
                pol = pol["holdingSummaries"][0]
                fund = requests.get(
                    os.getenv("OKAPI_URL") + "/orders/order-lines/" + pol["poLineId"],
                    headers=headers,
                ).json()
                if "fundDistribution" in fund:
                    for f in fund["fundDistribution"]:
                        if "code" in f and f["code"] in plate_funds:
                            print(f["code"])
                            item["fund"] = f["code"]


            holdings_record = _get_holdings_record(holdings, headers)

            # Retrieves permanent location id and name from Holdings along instance id
            permanent_location_id, permanent_location, instance_id = _retrieve_permanent_location(
                holdings_record, headers
            )

            item["effectiveLocation"] = {
                "id": permanent_location_id,
                "name": permanent_location,
            }


            # Trim spaces from call number components
            prefix, suffix = _trim_callno_components(item=item)

            if replace:
                # String replacement for call number prefix & suffix
                # -- replacements managed via CSV in repo
                with open("./prefix-suffix.csv", newline="") as csvfile:
                    reader = csv.DictReader(csvfile)
                    replacements = [row for row in reader]

                prefix_regex = _reps_to_regex(replacements=replacements, field="prefix")
                suffix_regex = _reps_to_regex(replacements=replacements, field="suffix")

                if prefix:
                    processed_prefix = _replace_string(
                        string=prefix, regex=prefix_regex
                    )
                    item["effectiveCallNumberComponents"]["prefix"] = processed_prefix

                if suffix:
                    processed_suffix = _replace_string(
                        string=suffix, regex=suffix_regex
                    )
                    item["effectiveCallNumberComponents"]["suffix"] = processed_suffix

            xml_raw = json2xml.Json2xml(item, wrapper="item").to_xml()

            

        except IndexError:
            xml_raw = etree.tostring(
                E.error(E.message(f"No item found for barcode {barcode}"))
            )

        if transform:
            # Transform XML to align with ALMA's RESTful API response
            transform = etree.XSLT(etree.parse("./alma-rest-item.xsl"))
            result = transform(etree.fromstring(xml_raw))

            # Retrieves instance and updates XML
            result = _instance_xml(result, instance_id, headers)

            # Updates Call Number Type based on holdings
            result = _set_callno_type(holdings_record, result, headers)

            xml = bytes(result)

        else:
            xml = xml_raw

        return Response(content=xml, media_type="application/xml")


def _get_collection_name(instance: dict, headers: dict) -> str:
    """
    Retrieves Collection Name from the instance
    """
    collection_note = ""
    note_result = requests.get(
        f"""{os.getenv('OKAPI_URL')}/instance-note-types?query=(name=="Collection name")""",
        headers=headers
    )
    note_result.raise_for_status()
    if len(note_result.json()['instanceNoteTypes']) < 1:
        return collection_note
    collection_note_id = note_result.json()['instanceNoteTypes'][0]['id']
    for note in instance.get("notes"):
        if note["instanceNoteTypeId"] == collection_note_id:
            collection_note = note["note"]
            break
    return collection_note


def _get_holdings_record(holdings_id: str, okapi_headers: dict) -> dict:
    holdings_result = requests.get(
        f"{os.getenv('OKAPI_URL')}/holdings-storage/holdings/{holdings_id}",
        headers=okapi_headers,
    )
    holdings_result.raise_for_status()
    return holdings_result.json()


def _instance_xml(xml: etree._XSLTResultTree, instance_id: str, headers: dict):
    """
    Retrieves FOLIO Instance and updates XML
    """
    instance_result = requests.get(
        f"{os.getenv('OKAPI_URL')}/inventory/instances/{instance_id}",
        headers=headers
    )
    instance_result.raise_for_status()
    instance = instance_result.json()

    # Sets specific XML elements
    date_of_publication_elem = xml.find("bib_data/date_of_publication")
    date_of_publication_elem.text = instance["publication"][0]['dateOfPublication']

    mms_id_elem = xml.find("bib_data/mms_id")
    mms_id_elem.text = instance["hrid"]

    public_note_elem = xml.find("item_data/public_note")
    public_note_elem.text = _get_collection_name(instance, headers)

    return xml


def _okapi_login():
    url = f"{os.getenv('OKAPI_URL')}/authn/login"
    headers = {
        "X-Okapi-Tenant": os.getenv("OKAPI_TENANT"),
    }
    data = {
        "username": os.getenv("OKAPI_USER"),
        "password": os.getenv("OKAPI_PASSWORD"),
    }
    r = requests.post(url, json=data, headers=headers)
    r.raise_for_status()
    if r.status_code == 201:
        return r.headers["X-Okapi-Token"]
    return None


def _reps_to_regex(replacements: List, field: str):
    return [
        (rf"^{rep['string']}$", f"{rep['replacement']}")
        for rep in replacements
        if rep["field"] == field
    ]


def _replace_string(string: str, regex: List):
    for r in regex:
        string = re.sub(r[0], r[1], string, flags=re.IGNORECASE)
    return string


def _retrieve_permanent_location(holdings_record: dict, okapi_headers: dict) -> tuple:
    """
    Returns uuid and name of the location based on the holdings record
    """
    permanent_location_id = holdings_record.get("permanentLocationId")
    instance_id = holdings_record.get('instanceId')
    if permanent_location_id is None:
        raise ValueError(f"Holding {holdings_id} missing permanent location")
    location_result = requests.get(
        f"{os.getenv('OKAPI_URL')}/locations/{permanent_location_id}",
        headers=okapi_headers,
    )
    location_result.raise_for_status()
    location_name = location_result.json().get("name")
    if location_name is None:
        raise ValueError(f"Location {permanent_location_id} missing Name")
    return permanent_location_id, location_name, instance_id


def _set_callno_type(holdings: dict, xml: etree._XSLTResultTree, okapi_headers: dict):
    """
    Updates Call Number Type desc property and value based on 
    """
    call_number_uuid = holdings['callNumberTypeId']
    call_number_type_result = requests.get(
        f"{os.getenv('OKAPI_URL')}/call-number-types/{call_number_uuid}",
        headers=okapi_headers
    )
    call_number_type_result.raise_for_status()
    name = call_number_type_result.json()["name"]
    code = None
    # Matches Ex Libris codes and names at https://developers.exlibrisgroup.com/alma/apis/docs/xsd/rest_item.xsd/?tags=GET#holding_data
    match name:
        case "Library of Congress classification":
            code = 0

        case "LC Modified":
            code = 0
            name = "Library of Congress classification"

        case "Dewey Decimal classification":
            code = 1

        case "National Library of Medicine classification":
            code = 2

        case "Superintendent of Documents classification":
            code = 3

        case "Shelving control number":
            code = 4

        case "Title":
            code = 5

        case "Shelved separately":
            code = 6

        case _:
            name = "Other scheme"
            code = 8

    call_number_type_elem = xml.find("holding_data/call_number_type")
    call_number_type_elem.set("desc", name)
    call_number_type_elem.text = str(code)

    return xml


def _trim_callno_components(item: dict):
    """
    Collapse multiple spaces to singular and remove leading/tailing spaces.
    Returns call number prefix and suffix for later string replacement.
    """
    callno_comps = item.get("effectiveCallNumberComponents", {})

    comps = {
        "callNumber": callno_comps.get("callNumber"),
        "prefix": callno_comps.get("prefix"),
        "suffix": callno_comps.get("suffix"),
    }

    for k, v in comps.items():
        if comps[k]:
            item["effectiveCallNumberComponents"][k] = comps[k] = re.sub(
                " +", " ", v
            ).strip()

    return comps["prefix"], comps["suffix"]


app.include_router(router)
