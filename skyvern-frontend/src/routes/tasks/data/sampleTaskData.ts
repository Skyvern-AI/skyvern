import { SampleCase } from "../types";

export const bci_seguros = {
  url: "https://www.bciseguros.cl/nuestros_seguros/personas/seguro-automotriz/",
  navigationGoal:
    "Generate an auto insurance quote. A quote has been generated when there's a table of coverages shown on the website.",
  dataExtractionGoal:
    "Extract ALL quote information in JSON format, with one entry per plan visible on the page. The output should include: the selected UF coverage value (3), auto plan name, the online price",
  navigationPayload: {
    Rut: "7.250.199-3",
    Sexo: "Masculino",
    "Fecha de Nacimiento": "03-02-2000",
    Telefono: "96908116",
    Comuna: "Lo Barnachea",
    "e-mail": "notarealemail@gmail.com",
    estado: "Usado",
    patente: "HZZV68",
    marca: "Subaru",
    modelo: "XV",
    ano: "2016",
    "tipo de combustible": "Bencina",
    "km approx a recorrer": "28,000",
  },
};

export const california_edd = {
  url: "https://eddservices.edd.ca.gov/acctservices/AccountManagement/AccountServlet?Command=NEW_SIGN_UP",
  navigationGoal:
    "Navigate through the employer services online enrollment form. Terminate when the form is completed",
  navigationPayload: {
    username: "isthisreal1",
    password: "Password123!",
    first_name: "John",
    last_name: "Doe",
    pin: "1234",
    email: "isthisreal1@gmail.com",
    phone_number: "412-444-1234",
  },
};

export const finditparts = {
  url: "https://www.finditparts.com",
  navigationGoal:
    "Search for the specified product id, add it to cart and then navigate to the cart page",
  dataExtractionGoal:
    "Extract all product quantity information from the cart page",
  navigationPayload: {
    product_id: "W01-377-8537",
  },
};

export const geico = {
  url: "https://www.geico.com",
  navigationGoal:
    "Navigate through the website until you generate an auto insurance quote. Do not generate a home insurance quote. If this page contains an auto insurance quote, consider the goal achieved",
  dataExtractionGoal:
    "Extract all quote information in JSON format including the premium amount, the timeframe for the quote.",
  navigationPayload: {
    licensed_at_age: 19,
    education_level: "HIGH_SCHOOL",
    phone_number: "8042221111",
    full_name: "Chris P. Bacon",
    past_claim: [],
    has_claims: false,
    spouse_occupation: "Florist",
    auto_current_carrier: "None",
    home_commercial_uses: null,
    spouse_full_name: "Amy Stake",
    auto_commercial_uses: null,
    requires_sr22: false,
    previous_address_move_date: null,
    line_of_work: null,
    spouse_age: "1987-12-12",
    auto_insurance_deadline: null,
    email: "chris.p.bacon@abc.com",
    net_worth_numeric: 1000000,
    spouse_gender: "F",
    marital_status: "married",
    spouse_licensed_at_age: 20,
    license_number: "AAAAAAA090AA",
    spouse_license_number: "AAAAAAA080AA",
    how_much_can_you_lose: 25000,
    vehicles: [
      {
        annual_mileage: 10000,
        commute_mileage: 4000,
        existing_coverages: null,
        ideal_coverages: {
          bodily_injury_per_incident_limit: 50000,
          bodily_injury_per_person_limit: 25000,
          collision_deductible: 1000,
          comprehensive_deductible: 1000,
          personal_injury_protection: null,
          property_damage_per_incident_limit: null,
          property_damage_per_person_limit: 25000,
          rental_reimbursement_per_incident_limit: null,
          rental_reimbursement_per_person_limit: null,
          roadside_assistance_limit: null,
          underinsured_motorist_bodily_injury_per_incident_limit: 50000,
          underinsured_motorist_bodily_injury_per_person_limit: 25000,
          underinsured_motorist_property_limit: null,
        },
        ownership: "Owned",
        parked: "Garage",
        purpose: "commute",
        vehicle: {
          style: "AWD 3.0 quattro TDI 4dr Sedan",
          model: "A8 L",
          price_estimate: 29084,
          year: 2015,
          make: "Audi",
        },
        vehicle_id: null,
        vin: null,
      },
    ],
    additional_drivers: [],
    home: [
      {
        home_ownership: "owned",
      },
    ],
    spouse_line_of_work: "Agriculture, Forestry and Fishing",
    occupation: "Customer Service Representative",
    id: null,
    gender: "M",
    credit_check_authorized: false,
    age: "1987-11-11",
    license_state: "Washington",
    cash_on_hand: "$10000–14999",
    address: {
      city: "HOUSTON",
      country: "US",
      state: "TX",
      street: "9625 GARFIELD AVE.",
      zip: "77082",
    },
    spouse_education_level: "MASTERS",
    spouse_email: "amy.stake@abc.com",
    spouse_added_to_auto_policy: true,
  },
  extractedInformationSchema: {
    additionalProperties: false,
    properties: {
      quotes: {
        items: {
          additionalProperties: false,
          properties: {
            coverages: {
              items: {
                additionalProperties: false,
                properties: {
                  amount: {
                    description:
                      "The coverage amount in USD, which can be a single value or a range (e.g., '$300,000' or '$300,000/$300,000').",
                    type: "string",
                  },
                  included: {
                    description:
                      "Indicates whether the coverage is included in the policy (true or false).",
                    type: "boolean",
                  },
                  type: {
                    description:
                      "The limit of the coverage (e.g., 'bodily_injury_limit', 'property_damage_limit', 'underinsured_motorist_bodily_injury_limit').\nTranslate the english name of the coverage to snake case values in the following list:\n  * bodily_injury_limit\n  * property_damage_limit\n  * underinsured_motorist_bodily_injury_limit\n  * personal_injury_protection\n  * accidental_death\n  * work_loss_exclusion\n",
                    type: "string",
                  },
                },
                type: "object",
              },
              type: "array",
            },
            premium_amount: {
              description:
                "The total premium amount for the whole quote timeframe in USD, formatted as a string (e.g., '$321.57').",
              type: "string",
            },
            quote_number: {
              description:
                "The quote number generated by the carrier that identifies this quote",
              type: "string",
            },
            timeframe: {
              description:
                "The duration of the coverage, typically expressed in months or years.",
              type: "string",
            },
            vehicle_coverages: {
              items: {
                additionalProperties: false,
                properties: {
                  collision_deductible: {
                    description:
                      "The collision deductible amount in USD, which is a single value (e.g., '$500') or null if it is not included",
                    type: "string",
                  },
                  comprehensive_deductible: {
                    description:
                      "The collision deductible amount in USD, which is a single value (e.g., '$500') or null if it is not included",
                    type: "string",
                  },
                  for_vehicle: {
                    additionalProperties: false,
                    description:
                      "The vehicle that the collision and comprehensive coverage is for",
                    properties: {
                      make: {
                        description: "The make of the vehicle",
                        type: "string",
                      },
                      model: {
                        description: "The model of the vehicle",
                        type: "string",
                      },
                      year: {
                        description: "The year of the vehicle",
                        type: "string",
                      },
                    },
                    type: "object",
                  },
                  underinsured_property_damage: {
                    description:
                      "The underinsured property damage limit for this vehicle, which is a limit and a deductible (e.g., '$25,000/$250 deductible') or null if it is not included",
                    type: "string",
                  },
                },
                type: "object",
              },
              type: "array",
            },
          },
          type: "object",
        },
        type: "array",
      },
    },
    type: "object",
  },
};

export function getSampleForInitialFormValues(sample: SampleCase) {
  switch (sample) {
    case "geico":
      return {
        ...geico,
        navigationPayload: JSON.stringify(geico.navigationPayload, null, 2),
        extractedInformationSchema: JSON.stringify(
          geico.extractedInformationSchema,
          null,
          2,
        ),
      };
    case "finditparts":
      return {
        ...finditparts,
        navigationPayload: JSON.stringify(
          finditparts.navigationPayload,
          null,
          2,
        ),
      };
    case "california_edd":
      return {
        ...california_edd,
        navigationPayload: JSON.stringify(
          california_edd.navigationPayload,
          null,
          2,
        ),
      };
    case "bci_seguros":
      return {
        ...bci_seguros,
        navigationPayload: JSON.stringify(
          bci_seguros.navigationPayload,
          null,
          2,
        ),
      };
  }
}
