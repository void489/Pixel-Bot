import { defineRailway, project, service } from "railway/iac";

// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "parrots helper";

export default defineRailway(() => {
  const parrots_helper = service("parrots helper", {
    start: "python main.py",
    // builder from CaC: "NIXPACKS"
  });
  return project("honest-comfort", {
    resources: [parrots_helper],
  });
});
