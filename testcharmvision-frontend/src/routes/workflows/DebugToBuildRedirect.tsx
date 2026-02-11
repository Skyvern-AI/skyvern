import { Navigate, useLocation } from "react-router-dom";

function DebugToBuildRedirect() {
  const location = useLocation();
  return (
    <Navigate
      to={
        location.pathname.replace(/\/debug(\/|$)/, "/build$1") +
        location.search +
        location.hash
      }
      replace
    />
  );
}

export { DebugToBuildRedirect };
