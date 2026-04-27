export const HomepageHero = () => {
  return (
    <div
      style={{
        padding: "48px 0 40px 0",
        textAlign: "center",
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "8px",
          padding: "6px 16px",
          borderRadius: "20px",
          backgroundColor: "rgba(91, 138, 140, 0.08)",
          border: "1px solid rgba(91, 138, 140, 0.2)",
          fontSize: "13px",
          color: "#5B8A8C",
          marginBottom: "24px",
          fontWeight: 500,
        }}
      >
        Open source on GitHub
      </div>
      <h1
        style={{
          fontSize: "42px",
          fontWeight: 700,
          lineHeight: 1.15,
          color: "#1a1a1a",
          margin: "0 0 16px 0",
          letterSpacing: "-0.02em",
        }}
      >
        Automate any browser workflow with AI
      </h1>
      <p
        style={{
          fontSize: "18px",
          lineHeight: 1.6,
          color: "#6B6560",
          maxWidth: "600px",
          margin: "0 auto 32px auto",
        }}
      >
        Describe what you want in plain English. Skyvern opens a real browser,
        reads the page visually, and completes the task.
      </p>
      <div
        style={{
          display: "flex",
          gap: "12px",
          justifyContent: "center",
          flexWrap: "wrap",
        }}
      >
        <a
          href="/getting-started/quickstart"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            padding: "12px 24px",
            borderRadius: "8px",
            backgroundColor: "#D4733B",
            color: "#fff",
            fontSize: "15px",
            fontWeight: 600,
            textDecoration: "none",
            transition: "background-color 0.15s ease",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "#C0632F")}
          onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "#D4733B")}
        >
          Get started with the API
        </a>
        <a
          href="/cloud/getting-started/overview"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "6px",
            padding: "12px 24px",
            borderRadius: "8px",
            backgroundColor: "transparent",
            color: "#2C2C2C",
            fontSize: "15px",
            fontWeight: 600,
            textDecoration: "none",
            border: "1px solid #E5DDD4",
            transition: "all 0.15s ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = "#5B8A8C";
            e.currentTarget.style.color = "#5B8A8C";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = "#E5DDD4";
            e.currentTarget.style.color = "#2C2C2C";
          }}
        >
          Use the dashboard
        </a>
      </div>
    </div>
  );
};
