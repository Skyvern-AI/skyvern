import { Button } from "./components/ui/Button";
import { ThemeProvider } from "@/components/ThemeProvider";

function App() {
  return (
    <ThemeProvider defaultTheme="dark" storageKey="skyvern-theme">
      <div className="h-screen w-screen flex items-center justify-center">
        <Button>Hello Skyvern!</Button>
      </div>
    </ThemeProvider>
  );
}

export default App;
