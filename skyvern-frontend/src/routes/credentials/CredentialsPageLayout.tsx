import { Outlet } from "react-router-dom";

function CredentialsPageLayout() {
  return (
    <div className="container mx-auto">
      <main>
        <Outlet />
      </main>
    </div>
  );
}

export { CredentialsPageLayout };
