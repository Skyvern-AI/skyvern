import net from "node:net";
let attempts = 0;
function refuse() {
  attempts++;
  throw new Error("TEST_NETWORK_FORBIDDEN");
}
globalThis.fetch = async () => refuse();
net.Socket.prototype.connect = refuse;
process.on("exit", () => {
  if (attempts !== 0) process.exitCode = 99;
});
