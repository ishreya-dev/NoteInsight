import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  onAuthStateChanged,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signOut as firebaseSignOut,
  type User as FirebaseUser,
} from "firebase/auth";

import { auth } from "./firebase";
import {
  AuthContext,
  type AuthContextValue,
} from "./AuthContext";

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({
  children,
}: AuthProviderProps) {
  const [user, setUser] =
    useState<FirebaseUser | null>(null);

  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(
      auth,
      (firebaseUser) => {
        setUser(firebaseUser);
        setLoading(false);
      },
    );

    return unsubscribe;
  }, []);

  const signIn = useCallback(
    async (email: string, password: string) => {
      await signInWithEmailAndPassword(
        auth,
        email,
        password,
      );
    },
    [],
  );

  const signUp = useCallback(
    async (email: string, password: string) => {
      await createUserWithEmailAndPassword(
        auth,
        email,
        password,
      );
    },
    [],
  );

  const signOut = useCallback(async () => {
    await firebaseSignOut(auth);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      signIn,
      signUp,
      signOut,
    }),
    [
      user,
      loading,
      signIn,
      signUp,
      signOut,
    ],
  );

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}