import { createServer } from "http";
import handler from "serve-handler";
import open from "open";

const port = 8080;
const url = `http://localhost:${port}`;

const server = createServer((request, response) => {
  const start = Date.now();

  // Hook into response finish to log status
  response.on("finish", () => {
    const duration = Date.now() - start;
    const timestamp = new Date().toISOString();
    console.log(
      "[%s] %s %s %d %dms",
      timestamp,
      request.method,
      request.url,
      response.statusCode,
      duration,
    );
  });

  // You pass two more arguments for config and middleware
  // More details here: https://github.com/vercel/serve-handler#options
  return handler(request, response, {
    public: "dist",
    rewrites: [
      {
        source: "**",
        destination: "/index.html",
      },
    ],
  });
});

server.listen(8080, async () => {
  console.log(`[${new Date().toISOString()}] Frontend server running at ${url}`);
  await open(url);
});
