import { Link } from "react-router-dom";

const recipes = [
  {
    title: "Healthcare",
    description: "Automate work with healthcare websites.",
    to: "/recipes/healthcare",
  },
  {
    title: "Government",
    description: "Navigate and complete tasks across government websites.",
    to: "/recipes/government",
  },
  {
    title: "Invoices",
    description: "Collect and download invoices with agents.",
    to: "/recipes/invoices",
  },
  {
    title: "Insurance",
    description: "Automate work with insurance websites.",
    to: "/recipes/insurance",
  },
  {
    title: "Purchasing",
    description: "Make payments and complete purchasing workflows on the web.",
    to: "/recipes/purchasing",
  },
  {
    title: "CRM",
    description: "Navigate and update records across CRM systems.",
    to: "/recipes/crm",
  },
  {
    title: "Logistics",
    description: "Automate work with logistics websites.",
    to: "/recipes/logistics",
  },
  {
    title: "Contact Forms",
    description: "Submit contact forms across websites.",
    to: "/recipes/contact-forms",
  },
  {
    title: "Job Apps",
    description: "Automate job applications with agents.",
    to: "/recipes/job-apps",
  },
];

function RecipesPage() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-normal text-neutral-950 dark:text-neutral-50">
          Recipes
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-neutral-600 dark:text-neutral-400">
          Browse ready-made agent templates for common web automation workflows.
        </p>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {recipes.map((recipe) => (
          <Link
            key={recipe.to}
            to={recipe.to}
            className="group rounded-lg border border-neutral-200 bg-white p-4 transition-colors duration-100 hover:border-neutral-300 hover:bg-neutral-50 dark:border-white/[0.08] dark:bg-neutral-950 dark:hover:border-white/[0.16] dark:hover:bg-white/[0.03]"
          >
            <div className="text-sm font-semibold text-neutral-950 group-hover:text-neutral-900 dark:text-neutral-100 dark:group-hover:text-white">
              {recipe.title}
            </div>
            <div className="mt-2 text-sm leading-5 text-neutral-600 dark:text-neutral-400">
              {recipe.description}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

export { RecipesPage };
