import type { Patient, SafetyBanner } from "@/types/api";

/**
 * The safety banner's data from a patient record: active alerts, criticals
 * marked. One builder, so every screen that shows the banner agrees on what is
 * in it — the patient record, and the lab bench (UX-04).
 */
export function safetyBannerFor(patient: Patient): SafetyBanner {
  const active = (patient.alerts ?? []).filter((alert) => alert.is_active);
  return {
    patient_id: patient.id,
    uhid: patient.uhid,
    full_name: patient.full_name,
    age_years: patient.age_years ?? null,
    gender: patient.gender,
    blood_group: patient.blood_group,
    is_deceased: patient.is_deceased,
    alerts: active.map((alert) => (alert.detail ? `${alert.label} — ${alert.detail}` : alert.label)),
    has_critical_alert: active.some((alert) => alert.severity === "CRITICAL"),
  };
}
