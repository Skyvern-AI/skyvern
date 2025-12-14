import { useParams } from "react-router-dom";

/**
 * Given a list of parameter names, returns the value of the first one that exists in the URL parameters.
 */
const useFirstParam = (...paramNames: string[]) => {
  const params = useParams();
  for (const name of paramNames) {
    const value = params[name];
    if (value) {
      return value;
    }
  }
  return undefined;
};

export { useFirstParam };
