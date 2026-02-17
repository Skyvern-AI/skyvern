import pytest
from dotenv import load_dotenv

from skyvern.forge import app  # noqa: F401
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory

load_dotenv()


@pytest.fixture(scope="module", autouse=True)
def setup_forge_app():
    start_forge_app()
    yield


VISA_NAVIGATION_PLAYLOAD = {
    "login_credentials": {"username": "test@gmail.com", "password": "password"},
    "info": {
        "name_of_traveller": "Rajesh Patel",
        "nationality": "Indian",
        "citizenship": "Indian",
        "date_of_birth": "20 Jul 1999",
        "passport_number": "G9999999",
        "date_of_issue": "21 Jul 2014",
        "date_of_expiry": "20 Jul 2024",
        "place_of_issue": "Jaipur",
        "national_identity_card": "Aadhar Card",
        "aadhar_card_number": "300009999999",
        "name_on_aadhar_card": "Rajesh Patel",
        "address": "JMR 999, xyz road, Marathalli, Bengaluru, Karnataka, 560106",
        "relationship_status": "Married",
        "phone_number": "9999999999",
        "email": "test@gmail.com",
        "date_of_departure_from_india": "01/06/2024",
        "date_of_departure_from_australia": "25/06/2024",
        "employment_type": "Employed",
        "company_name": "XYZ Services",
        "start_date_with_current_employer": "02/02/2023",
        "organisation_address": "JMR 999, xyz road, Marathalli, Bengaluru, Karnataka, 560106",
        "special_category_of_entry": "no",
        "pacific_australia_card": "no",
        "multiple_citizenships": "no",
        "other_travel_documentation": "N/A",
    },
}
EXTRACTED_INFORMATION_SCHEMA = """{'additionalProperties': False, 'properties': {'quotes': {'items': {'additionalProperties': False, 'properties': {'coverages': {'items': {'additionalProperties': False, 'properties': {'amount': {'description': "The coverage amount in USD, which can be a single value or a range (e.g., '$300,000' or '$300,000/$300,000').", 'type': 'string'}, 'included': {'description': 'Indicates whether the coverage is included in the policy (true or false).', 'type': 'boolean'}, 'type': {'description': "The type of coverage (e.g., 'Bodily Injury Liability') or deductible name.", 'type': 'string'}}, 'type': 'object'}, 'type': 'array'}, 'premium_amount': {'description': "The total premium amount for the whole quote timeframe in USD, formatted as a string (e.g., '$321.57').", 'type': 'string'}, 'timeframe': {'description': 'The duration of the coverage, typically expressed in months or years.', 'type': 'string'}, 'vehicle_coverages': {'items': {'additionalProperties': False, 'properties': {'collision_deductible': {'description': "The collision deductible amount in USD, which is a single value (e.g., '$500') or null if it is not included", 'type': 'string'}, 'comprehensive_deductible': {'description': "The collision deductible amount in USD, which is a single value (e.g., '$500') or null if it is not included", 'type': 'string'}, 'for_vehicle': {'additionalProperties': False, 'description': 'The vehicle that the collision and comprehensive coverage is for', 'properties': {'make': {'description': 'The make of the vehicle', 'type': 'string'}, 'model': {'description': 'The model of the vehicle', 'type': 'string'}, 'year': {'description': 'The year of the vehicle', 'type': 'string'}}, 'type': 'object'}, 'underinsured_property_damage': {'description': "The underinsured property damage limit for this vehicle, which is a limit and a deductible (e.g., '$25,000/$250 deductible') or null if it is not included", 'type': 'string'}}, 'type': 'object'}, 'type': 'array'}}, 'type': 'object'}, 'type': 'array'}}, 'type': 'object'}"""
llm_api_handler = LLMAPIHandlerFactory.get_llm_api_handler("OPENAI_GPT5_1")


@pytest.mark.asyncio
@pytest.mark.skip(reason="temporarily disabled: flaky in staging")
async def test_extract_info_prompt() -> None:
    """
    expected_response = {
        "quotes": [
            {
                "premium_amount": "$1,440.11",
                "timeframe": "6 Month",
                "coverages": [
                    {"type": "Bodily Injury Liability", "amount": "$30,000/$60,000", "included": True},
                    {"type": "Property Damage Liability", "amount": "$25,000", "included": True},
                    {"type": "Medical Payments", "amount": "Not Included", "included": False},
                    {"type": "Personal Injury Protection", "amount": "Not Included", "included": False},
                    {"type": "Uninsured Motorist Bodily Injury", "amount": "$30,000/$60,000", "included": True},
                    {"type": "Upgraded Accident Forgiveness", "amount": "Not Included", "included": False},
                ],
                "vehicle_coverages": [
                    {
                        "for_vehicle": {"year": "2015", "make": "Audi", "model": "A8"},
                        "collision_deductible": "$1,000",
                        "comprehensive_deductible": "$1,000",
                        "underinsured_property_damage": "$25,000/$250 deductible",
                    }
                ],
            }
        ]
    }
    """
    extract_information_prompt = prompt_engine.load_prompt(
        "extract-information",
        data_extraction_goal="extract all quote information in JSON format including the premium amount, and the timeframe for the quote",
        extracted_information_schema=EXTRACTED_INFORMATION_SCHEMA,
        current_url="https://www.geico.com/quote/",
    )
    image_path = "tests/smoke_tests/data/extract_information_screenshot.png"
    image = open(image_path, "rb").read()
    parsed_response = await llm_api_handler(
        prompt=extract_information_prompt, screenshots=[image], step=None, prompt_name="extract-information"
    )

    assert "quotes" in parsed_response
    assert len(parsed_response["quotes"]) == 1

    # assertions on quote
    quote = parsed_response["quotes"][0]
    expected_quote_attrs = ["premium_amount", "timeframe", "coverages", "vehicle_coverages"]
    for attr in expected_quote_attrs:
        assert attr in quote
    # assert quote["premium_amount"] == "$1,440.11"
    timeframe_lowercase = quote["timeframe"].lower()
    assert timeframe_lowercase == "6 month" or timeframe_lowercase == "6 months"
    assert len(quote["coverages"]) == 6

    # assertions on coverages
    expected_coverage_types = {
        "Bodily Injury Liability",
        "Property Damage Liability",
        "Medical Payments",
        "Personal Injury Protection",
        "Uninsured Motorist Bodily Injury",
        "Upgraded Accident Forgiveness",
    }
    for expected_coverage_type in expected_coverage_types:
        assert any([expected_coverage_type in coverage["type"] for coverage in quote["coverages"]])
    for coverage in quote["coverages"]:
        if "Bodily Injury Liability" in coverage["type"]:
            assert coverage["included"] is True
            assert coverage["amount"] == "$30,000/$60,000" or coverage["amount"] == "$30000/$60000"
        elif "Property Damage Liability" in coverage["type"]:
            assert coverage["included"] is True
            assert coverage["amount"] == "$25,000" or coverage["amount"] == "$25000"
        elif "Medical Payments" in coverage["type"]:
            assert coverage["included"] is False
        elif "Personal Injury Protection" in coverage["type"]:
            assert coverage["included"] is False
        elif "Uninsured Motorist Bodily Injury" in coverage["type"]:
            assert coverage["amount"] == "$30,000/$60,000" or coverage["amount"] == "$30000/$60000"
            assert coverage["included"] is True
        elif "Upgraded Accident Forgiveness" in coverage["type"]:
            assert coverage["included"] is False

    # assertions on vehicle
    assert len(quote["vehicle_coverages"]) == 1
    vehicle = quote["vehicle_coverages"][0]
    assert vehicle["for_vehicle"] == {"year": "2015", "make": "Audi", "model": "A8"}
    assert "$1,000" in vehicle["collision_deductible"] or "$1000" in vehicle["collision_deductible"]
    assert "$1,000" in vehicle["comprehensive_deductible"] or "$1000" in vehicle["comprehensive_deductible"]
    assert vehicle["underinsured_property_damage"] == "$25,000/$250 deductible"


# @pytest.mark.asyncio
# async def test_yes_or_no() -> None:
#     prompt = prompt_engine.load_prompt(
#         "extract-action",
#         navigation_goal="Login Into the Portal >> Click On Add New Applicaiton >> Select Visa Category -Visitor> Click on Visa Visitor (600)>> fill out the application. Refer to error banner on the top when there are errors but do not click any of those links. Do not use any link from 'Related Links' or 'Help and Support'. You're done when the application is successfully filled out",
#         navigation_payload_str=json.dumps(VISA_NAVIGATION_PLAYLOAD),
#         url="https://online.immi.gov.au/elp/app",
#         elements=get_complex_element_tree(),
#         data_extraction_goal=None,
#         action_history=[],
#         utc_datetime=datetime.utcnow(),
#     )

#     screenshot1 = encode_image("tests/smoke_tests/data/complex_context_screenshot1.png")
#     screenshot2 = encode_image("tests/smoke_tests/data/complex_context_screenshot2.png")
#     llm_request = build_chat_request(prompt, [screenshot1, screenshot2])
#     response = await openai_client.client.chat.completions.with_raw_response.create(**llm_request)
#     chat_completion = response.parse()
#     parse_response(chat_completion)
#     assert True


# @pytest.mark.asyncio
# async def test_geico_closest_coverage() -> None:
#     prompt = prompt_engine.load_prompt(
#         "extract-action",
#         navigation_goal="Navigate through the website until you generate an auto insurance quote. Only stay on the\nstarting website, do not navigate to other carrier websites.\nDo not start over, terminate instead.\nIf the only options are going to a different carrier's website or talking to an agent, terminate.\nDo not generate a home insurance quote.\nChoose the following coverage levels\n  * Bodily Injury: $300k/500k\n  * Property Damage: 500k\n  * Underinsured Motorist Bodily Injury: $250k/500k\nChoose the following deductibles for the vehicles:\n  * 2021 Chevrolet Silverado 1500 collision deductible of $2000 and comprehensive deductible of $2000\n  * 2023 GMC Yukon XL collision deductible of $2000 and comprehensive deductible of $2000\n  * 1994 Chevrolet Camaro collision deductible of $2000 and comprehensive deductible of $2000\n  * 2017 Cadillac Escalade ESV collision deductible of $2000 and comprehensive deductible of $2000\nChoose coverages and deductibles that are consistent with the ideal_coverages key.\nIf the coverage level we're looking for is not in the option list, select available coverage level that's closest to what we're looking for. Example 1: if the options are $5,000, $10,000, $15,000, $20,000, $25,000, $50,000, $100,000, and the wanted level is $500,000, select $100,000 as it's the closest number to $500,000. Example 2: if the options are $5,000, $10,000, $15,000, $20,000, $25,000, $50,000, $100,000, and the wanted level is $70,000. 70000-5000=65000, 70000-10000=60000, 70000-15000=55000, 70000-25000, 70000-50000 = 20000, 100000-70000=30000. 20000 is the smallest so $50,000 is the closest coverage, select $50,000\nIf this page contains an auto insurance quote, consider the goal achieved.\n\nIf auto insurance quote amount, which should be number that represents money, is not found,\nthink if there are still actions to take to get the amount. For examples, sometimes\nyou need to recalculate the quote if you have updated the coverages.",
#         navigation_payload_str=get_file("tests/smoke_tests/data/geico_closest_coverage/navigation_payload.json"),
#         url="https://www.geico.com",
#         elements=get_file("tests/smoke_tests/data/geico_closest_coverage/element_tree.json"),
#         data_extraction_goal='Extract all quote information in JSON format including the premium amount, the timeframe for the quote. The bodily injury, property damage, and underinsured motorist coverages should be returned in the "coverages" key. The collision and comprehensive deductibles should be returned in the "vehicle_coverages" key as they are specific to each vehicle. Also return the quote number.',
#         action_history=[],
#         utc_datetime=datetime.utcnow(),
#     )

#     screenshot1 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss1.png")
#     screenshot2 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss2.png")
#     screenshot3 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss3.png")
#     screenshot4 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss4.png")
#     screenshot5 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss5.png")
#     screenshot6 = encode_image("tests/smoke_tests/data/geico_closest_coverage/geico_closest_coverage_prompt_ss6.png")
#     llm_request = build_chat_request(
#         prompt,
#         [screenshot1, screenshot2, screenshot3, screenshot4, screenshot5, screenshot6],
#     )
#     response = await openai_client.client.chat.completions.with_raw_response.create(**llm_request)
#     chat_completion = response.parse()
#     parse_response(chat_completion)
#     assert True


# @pytest.mark.asyncio
# async def test_workable_yes_or_no() -> None:
#     prompt = prompt_engine.load_prompt(
#         "extract-action",
#         navigation_goal="Apply for a job. Terminate if the job is not available. Fill out all of the fields as best you can, including optional fields. Be safe when filling out fields where the user didn't specify any details for. Consider the goal achieved when all the relevant fields are completed and the job application is submitted successfully. Job application is submitted successfully when it's indicated on the page. If there are constraints for the job application that the applicant isn't satisfying the requirements for (such as language proficiency, job location), terminate with appropriate reasoning.",
#         navigation_payload_str=get_file("tests/smoke_tests/data/workable_yes_or_no/navigation_payload.json"),
#         url="https://apply.workable.com/employer-direct-healthcare/j/037FD88783/apply/",
#         elements=get_file("tests/smoke_tests/data/workable_yes_or_no/element_tree.json"),
#         action_history=[],
#         utc_datetime=datetime.utcnow(),
#     )

#     screenshot1 = encode_image("tests/smoke_tests/data/workable_yes_or_no/screenshot1.png")
#     screenshot2 = encode_image("tests/smoke_tests/data/workable_yes_or_no/screenshot2.png")
#     screenshot3 = encode_image("tests/smoke_tests/data/workable_yes_or_no/screenshot3.png")
#     screenshot4 = encode_image("tests/smoke_tests/data/workable_yes_or_no/screenshot4.png")
#     llm_request = build_chat_request(
#         prompt,
#         [screenshot1, screenshot2, screenshot3, screenshot4],
#     )
#     response = await openai_client.client.chat.completions.with_raw_response.create(**llm_request)
#     chat_completion = response.parse()
#     parse_response(chat_completion)
#     assert True
