package org.wms.auth;

import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

public final class PasswordRevocationFactory implements EventListenerProviderFactory {
    @Override public EventListenerProvider create(KeycloakSession session) { return new PasswordRevocation(session); }
    @Override public String getId() { return "wms-password-revocation"; }
    @Override public void init(Config.Scope config) { }
    @Override public void postInit(KeycloakSessionFactory factory) { }
    @Override public void close() { }
}
