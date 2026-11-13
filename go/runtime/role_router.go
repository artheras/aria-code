package main

type AgentRole string

const (
	RolePlanner  AgentRole = "planner"
	RoleCoder    AgentRole = "coder"
	RoleReviewer AgentRole = "reviewer"
	RoleResearch AgentRole = "researcher"
)

type roleRouter struct {
	baseProvider modelProvider
	cfg          config
	providers    map[AgentRole]modelProvider
}

func newRoleRouter(base modelProvider, c config) *roleRouter {
	return &roleRouter{
		baseProvider: base,
		cfg:          c,
		providers:    make(map[AgentRole]modelProvider),
	}
}

func (rr *roleRouter) ProviderForRole(role AgentRole) modelProvider {
	if rr == nil {
		return nil
	}
	if p, ok := rr.providers[role]; ok && p != nil {
		return p
	}
	rolePrefix := string(role)
	roleModel := rr.cfg.text(rolePrefix+"_model", "")
	roleProvName := rr.cfg.text(rolePrefix+"_provider", "")
	if roleModel == "" && roleProvName == "" {
		return rr.baseProvider
	}
	roleCfg := make(config)
	for k, v := range rr.cfg {
		roleCfg[k] = v
	}
	if roleProvName != "" {
		roleCfg["local_provider"] = roleProvName
	}
	if roleModel != "" {
		roleCfg["model"] = roleModel
	}
	prov, err := modelProviderFor(roleCfg)
	if err != nil || prov == nil {
		return rr.baseProvider
	}
	rr.providers[role] = prov
	return prov
}
