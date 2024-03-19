import { Button } from "./components/ui/Button";
import { ThemeProvider } from "@/components/ThemeProvider";

function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="vite-ui-theme">
      <Button variant={"destructive"}>Hello Shadcn button!</Button>
    </ThemeProvider>
  );
}

export default App;
