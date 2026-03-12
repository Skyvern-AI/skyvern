const Lane = ({ title, description, links }) => {
  return (
    <div
      style={{
        padding: "24px",
        borderRadius: "12px",
        border: "1px solid #E5DDD4",
        backgroundColor: "#fff",
        transition: "border-color 0.15s ease, box-shadow 0.15s ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#7BA3A5";
        e.currentTarget.style.boxShadow = "0 4px 12px rgba(0,0,0,0.06)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "#E5DDD4";
        e.currentTarget.style.boxShadow = "none";
      }}
    >
      <h3
        style={{
          fontSize: "16px",
          fontWeight: 600,
          color: "#1a1a1a",
          margin: "0 0 6px 0",
        }}
      >
        {title}
      </h3>
      <p
        style={{
          fontSize: "14px",
          color: "#6B6560",
          margin: "0 0 20px 0",
          lineHeight: 1.5,
        }}
      >
        {description}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        {links.map((link, i) => (
          <a
            key={i}
            href={link.href}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "8px 12px",
              borderRadius: "6px",
              backgroundColor: "rgba(250, 246, 241, 0.6)",
              color: "#2C2C2C",
              fontSize: "14px",
              fontWeight: 500,
              textDecoration: "none",
              transition: "background-color 0.12s ease, color 0.12s ease",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.backgroundColor = "rgba(91, 138, 140, 0.08)";
              e.currentTarget.style.color = "#5B8A8C";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.backgroundColor = "rgba(250, 246, 241, 0.6)";
              e.currentTarget.style.color = "#2C2C2C";
            }}
          >
            <span>{link.label}</span>
            <span style={{ fontSize: "12px", opacity: 0.5 }}>→</span>
          </a>
        ))}
      </div>
    </div>
  );
};

export const PersonaLanes = () => {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
        gap: "16px",
        margin: "0 0 16px 0",
      }}
    >
      <Lane
        title="Automate without code"
        description="Use the dashboard to run tasks and build workflows visually."
        links={[
          { label: "Dashboard overview", href: "/cloud/getting-started/overview" },
          { label: "Run your first task", href: "/cloud/getting-started/run-your-first-task" },
          { label: "Build a workflow", href: "/cloud/building-workflows/build-a-workflow" },
          { label: "Connect to Zapier", href: "/integrations/zapier" },
        ]}
      />
      <Lane
        title="Build with the API"
        description="Integrate browser automation into your product with Python, TypeScript, or REST."
        links={[
          { label: "API quickstart", href: "/getting-started/quickstart" },
          { label: "Python SDK", href: "/sdk-reference/overview" },
          { label: "TypeScript SDK", href: "/ts-sdk-reference/overview" },
          { label: "API reference", href: "/api-reference" },
        ]}
      />
      <Lane
        title="Self-host"
        description="Run Skyvern on your own infrastructure with your own LLM keys."
        links={[
          { label: "Deployment overview", href: "/self-hosted/overview" },
          { label: "Docker setup", href: "/self-hosted/docker" },
          { label: "LLM configuration", href: "/self-hosted/llm-configuration" },
          { label: "Kubernetes", href: "/self-hosted/kubernetes" },
        ]}
      />
    </div>
  );
};
