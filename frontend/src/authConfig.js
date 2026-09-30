import { PublicClientApplication } from '@azure/msal-browser'

export const msalConfig = {
  auth: {
    clientId: import.meta.env.VITE_AZURE_CLIENT_ID,
    authority: `https://login.microsoftonline.com/${import.meta.env.VITE_AZURE_TENANT_ID}`,
    redirectUri: window.location.origin + (import.meta.env.VITE_REDIRECT_PATH || ''),
  },
  cache: {
    cacheLocation: 'sessionStorage',
    storeAuthStateInCookie: false,
  },
}

export const loginRequest = {
  scopes: ['User.Read'],
}

// Singleton — shared by main.jsx (initialize) and AuthWrapper.jsx (logoutRedirect)
export const msalInstance = new PublicClientApplication(msalConfig)
