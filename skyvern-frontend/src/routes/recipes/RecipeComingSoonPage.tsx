import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";

type Props = {
  title: string;
  description: string;
};

function RecipeComingSoonPage({ title, description }: Props) {
  return (
    <div>
      <h1 className="mb-5 text-3xl font-bold">{title}</h1>
      <h2 className="mb-5 text-neutral-600 dark:text-slate-400">
        {description}
      </h2>
      <div className="mt-24 flex w-full justify-center">
        <div className="flex w-[409px] flex-col items-center gap-4">
          <h1 className="text-3xl font-bold">Apply for Private Beta</h1>
          <h2 className="text-center text-neutral-600 dark:text-slate-400">
            This Agent is currently in private beta, book a demo to learn more.
          </h2>
          <Button size="lg" asChild>
            <Link
              to="https://www.skyvern.com/contact"
              target="_blank"
              rel="noopener noreferrer"
            >
              Book a Demo
            </Link>
          </Button>
        </div>
      </div>
    </div>
  );
}

export { RecipeComingSoonPage };
