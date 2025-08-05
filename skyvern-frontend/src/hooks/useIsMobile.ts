import { useEffect, useState } from "react";

function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const checkIsMobile = () => {
      // Check user agent for mobile phones specifically (not tablets)
      const userAgent = navigator.userAgent.toLowerCase();

      // Mobile phone patterns - exclude tablets
      const mobilePatterns = [
        /android.*mobile/, // Android phones (excludes tablets)
        /iphone/, // iPhone
        /ipod/, // iPod touch
        /blackberry/, // BlackBerry
        /windows phone/, // Windows Phone
        /opera mini/, // Opera Mini
        /iemobile/, // IE Mobile
        /mobile/, // Generic mobile (but will be filtered by screen size)
      ];

      // Check if user agent matches mobile patterns
      const hasMobileUserAgent = mobilePatterns.some((pattern) =>
        pattern.test(userAgent),
      );

      // Additional check: screen width for mobile phones (typically < 768px)
      // This helps distinguish phones from tablets
      const hasSmallScreen = window.innerWidth < 768;

      // Exclude tablets by checking for tablet-specific patterns
      const tabletPatterns = [
        /ipad/,
        /android(?!.*mobile)/, // Android tablets (Android without "mobile")
        /tablet/,
        /kindle/,
      ];

      const isTablet = tabletPatterns.some((pattern) =>
        pattern.test(userAgent),
      );

      // Return true only if it's a mobile device with small screen and not a tablet
      return hasMobileUserAgent && hasSmallScreen && !isTablet;
    };

    const handleResize = () => {
      setIsMobile(checkIsMobile());
    };

    // Initial check
    setIsMobile(checkIsMobile());

    // Listen for window resize events
    window.addEventListener("resize", handleResize);

    // Cleanup
    return () => {
      window.removeEventListener("resize", handleResize);
    };
  }, []);

  return isMobile;
}

export { useIsMobile };
