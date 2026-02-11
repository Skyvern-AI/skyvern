import { CreateNewTaskFormValues } from "../create/taskFormTypes";
import { SampleCase } from "../types";

export const blank = {
  url: "https://www.example.com",
  navigationGoal: "",
  dataExtractionGoal: "",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

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
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const california_edd = {
  url: "https://eddservices.edd.ca.gov/acctservices/AccountManagement/AccountServlet?Command=NEW_SIGN_UP",
  navigationGoal:
    "Navigate through the employer services online enrollment form. Terminate when the form is completed",
  dataExtractionGoal: null,
  navigationPayload: {
    username: "isthisreal1",
    password: "Password123!",
    first_name: "John",
    last_name: "Doe",
    pin: "1234",
    email: "isthisreal1@gmail.com",
    phone_number: "412-444-1234",
  },
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const finditparts = {
  url: "https://www.finditparts.com",
  navigationGoal:
    "Search for the specified product id, add it to cart and then navigate to the cart page. Your goal is COMPLETE when you're on the cart page and the specified product is in the cart. Do not attempt to checkout.",
  dataExtractionGoal:
    "Extract all product quantity information from the cart page",
  navigationPayload: {
    product_id: "W01-377-8537",
  },
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const contact_us_forms = {
  url: "https://canadahvac.com/contact-hvac-canada/",
  navigationGoal:
    "Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent.",
  navigationPayload: {
    name: "John Doe",
    email: "john.doe@gmail.com",
    phone: "123-456-7890",
    message: "Hello, I have a question about your services.",
  },
  dataExtractionGoal: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const job_application = {
  url: "https://jobs.lever.co/leverdemo-8/45d39614-464a-4b62-a5cd-8683ce4fb80a/apply",
  navigationGoal:
    "Fill out the job application form and apply to the job. Fill out any public burden questions if they appear in the form. Your goal is complete when the page says you've successfully applied to the job. Terminate if you are unable to apply successfully.",
  navigationPayload: {
    name: "John Doe",
    email: "john.doe@gmail.com",
    phone: "123-456-7890",
    resume_url:
      "https://writing.colostate.edu/guides/documents/resume/functionalSample.pdf",
    cover_letter: "Generate a compelling cover letter for me",
  },
  dataExtractionGoal: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const geico = {
  url: "https://www.geico.com",
  navigationGoal:
    "Navigate through the website until you generate an auto insurance quote. Do not generate a home insurance quote. If you're on a page showing an auto insurance quote (with premium amounts), your goal is COMPLETE",
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
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const hackernews = {
  url: "https://news.ycombinator.com",
  navigationGoal:
    "Navigate to the Hacker News homepage and identify the top post. COMPLETE when the title and URL of the top post are extracted. Ensure that the top post is the first post listed on the page.",
  dataExtractionGoal:
    "Extract the title and URL of the top post on the Hacker News homepage.",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const AAPLStockPrice = {
  url: "https://www.google.com/finance",
  navigationGoal:
    "Navigate to the search bar on Google Finance, type 'AAPL', and press Enter. COMPLETE when the search results for AAPL are displayed and the stock price is extracted.",
  dataExtractionGoal: "Extract the stock price for AAPL",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const NYTBestseller = {
  url: "https://www.nytimes.com/books/best-sellers",
  navigationGoal:
    "Navigate to the NYT Bestsellers page and identify the top book listed. COMPLETE when the title and author of the top book are identified.",
  dataExtractionGoal:
    "Extract the title, author, and rating of the top NYT Bestseller from the page.",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const topRankedFootballTeam = {
  url: "https://www.fifa.com/fifa-world-ranking/",
  navigationGoal:
    "Navigate to the FIFA World Ranking page and identify the top ranked football team. COMPLETE when the name of the top ranked football team is found and displayed.",
  dataExtractionGoal:
    "Extract the name of the top ranked football team from the FIFA World Ranking page.",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export const extractIntegrationsFromGong = {
  url: "https://www.gong.io",
  navigationGoal:
    "Navigate to the 'Integrations' page on the Gong website. COMPLETE when the page displaying a list of integrations is fully loaded. Ensure not to click on any external links or advertisements.",
  dataExtractionGoal:
    "Extract the names and descriptions of all integrations listed on the Gong integrations page.",
  navigationPayload: null,
  extractedInformationSchema: null,
  webhookCallbackUrl: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  errorCodeMapping: null,
};

export function getSample(sample: SampleCase) {
  switch (sample) {
    case "geico": {
      return geico;
    }
    case "finditparts": {
      return finditparts;
    }
    case "contact_us_forms": {
      return contact_us_forms;
    }
    case "california_edd": {
      return california_edd;
    }
    case "bci_seguros": {
      return bci_seguros;
    }
    case "job_application": {
      // copy the object to avoid modifying the original. Update job_application.navigationPayload.email to a random email
      const email = generateUniqueEmail();
      const phone = generatePhoneNumber();
      return {
        ...job_application,
        navigationPayload: {
          ...job_application.navigationPayload,
          email,
          phone,
        },
      };
    }
    case "hackernews": {
      return hackernews;
    }
    case "AAPLStockPrice": {
      return AAPLStockPrice;
    }
    case "NYTBestseller": {
      return NYTBestseller;
    }
    case "topRankedFootballTeam": {
      return topRankedFootballTeam;
    }
    case "extractIntegrationsFromGong": {
      return extractIntegrationsFromGong;
    }
    case "blank": {
      return blank;
    }
  }
}

export function generateUniqueEmail() {
  // Define the characters to use for the random part
  const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
  let randomString = "";

  // Generate a random string of 8 characters
  for (let i = 0; i < 8; i++) {
    const randomIndex = Math.floor(Math.random() * chars.length);
    randomString += chars[randomIndex];
  }

  // Concatenate with '@example.com'
  const email = randomString + "@example.com";
  return email;
}

export function generatePhoneNumber() {
  let phoneNumber = "";

  // The first digit should be between 1 and 9 (it can't be 0)
  phoneNumber += Math.floor(Math.random() * 9) + 1;

  // The remaining 9 digits can be between 0 and 9
  for (let i = 0; i < 9; i++) {
    phoneNumber += Math.floor(Math.random() * 10);
  }

  return phoneNumber;
}

function transformKV([key, value]: [string, unknown]) {
  if (value !== null && typeof value === "object") {
    return [key, JSON.stringify(value, null, 2)];
  }
  return [key, value];
}

export function getSampleForInitialFormValues(
  sample: SampleCase,
): CreateNewTaskFormValues {
  return Object.fromEntries(Object.entries(getSample(sample)).map(transformKV));
}
