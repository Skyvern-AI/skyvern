declare module "fetch-to-curl" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function fetchToCurl(requestInfo: any, requestInit?: any): string;
  export default fetchToCurl;
  export { fetchToCurl };
}
