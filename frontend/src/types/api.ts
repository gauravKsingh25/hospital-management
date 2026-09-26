/**
 * Friendly names for the generated API types.
 *
 * `openapi.d.ts` next to this file is machine-written from the backend's
 * OpenAPI schema — never edit it. Regenerate with `npm run codegen`, and
 * `npm run codegen:check` fails the build when it is stale.
 *
 * ## Why generated rather than hand-written
 *
 * There are 248 schemas behind this. Hand-typing them would create a second
 * declaration of the same truth, and second declarations drift: a field goes
 * optional in Pydantic, the TypeScript still says required, and nothing tells
 * anyone until a screen renders `undefined` in front of a patient. The
 * backend already treats this class of bug seriously — `test_rbac_seed.py`
 * exists solely to catch a code/database drift of the same shape — so the
 * frontend should not reintroduce it by hand.
 *
 * This file exists only to give the generated names a readable surface:
 * `Patient` rather than `components["schemas"]["PatientDetail"]`. It adds no
 * types of its own, so there is nothing here to keep in step.
 */

import type { components } from "@/types/openapi";

type Schemas = components["schemas"];

// --- Identity & access -----------------------------------------------------
export type CurrentUser = Schemas["CurrentUserRead"];
export type User = Schemas["UserRead"];
export type Role = Schemas["RoleRead"];
export type TokenPairResponse = Schemas["TokenPair"];

// --- Patients --------------------------------------------------------------
export type Patient = Schemas["PatientDetail"];
export type PatientListItem = Schemas["PatientRead"];
export type PatientRegistration = Schemas["PatientRegister"];
export type RegistrationResult = Schemas["RegistrationResponse"];
export type DuplicateCandidate = Schemas["DuplicateCandidate"];
export type PatientAlert = Schemas["PatientAlertRead"];
export type Gender = Schemas["Gender"];
export type BloodGroup = Schemas["BloodGroup"];
export type AlertSeverity = Schemas["AlertSeverity"];

// --- Scheduling ------------------------------------------------------------
export type Doctor = Schemas["DoctorRead"];
export type QueueEntry = Schemas["QueueEntryRead"];
/** A queue row with the patient identity a worklist needs to be readable. */
export type QueueBoardEntry = Schemas["QueueBoardEntry"];
export type QueueSummary = Schemas["QueueSummary"];
/** One doctor's column on reception's board: counts plus the live rows. */
export type DoctorQueue = Schemas["DoctorQueue"];
export type QueueReassignRequest = Schemas["QueueReassignRequest"];
export type QuickOpdRequest = Schemas["QuickOpdRequest"];
export type QuickOpdResult = Schemas["QuickOpdResponse"];
export type QueueStatus = Schemas["QueueStatus"];
export type QueuePriority = Schemas["QueuePriority"];

// --- Universal search ------------------------------------------------------
/** One result from the cross-module search box (CLAUDE.md §7b). */
export type SearchHit = Schemas["SearchHit"];
export type SearchHitKind = Schemas["SearchHitKind"];
export type SearchResults = Schemas["SearchResults"];

// --- Tenancy & administration ----------------------------------------------
export type Hospital = Schemas["HospitalRead"];
export type Department = Schemas["DepartmentRead"];
export type DepartmentType = Schemas["DepartmentType"];
export type RateCard = Schemas["RateCardRead"];
export type ServiceItem = Schemas["ServiceItemRead"];
export type ServicePrice = Schemas["ServicePriceRead"];
export type ChargeCategory = Schemas["ChargeCategory"];
export type SpecimenType = Schemas["SpecimenType"];

// --- Inpatient (wards and beds are configured in administration) -----------
export type Ward = Schemas["WardRead"];
export type Bed = Schemas["BedRead"];
export type BedClass = Schemas["BedClass"];
export type BedStatus = Schemas["BedStatus"];
/** A ward and its beds, as the nursing station sees them. */
export type BoardWard = Schemas["BoardWard"];
export type BoardBed = Schemas["BoardBed"];
export type Occupancy = Schemas["OccupancyStats"];
export type Admission = Schemas["AdmissionRead"];
export type AdmissionSummary = Schemas["AdmissionSummary"];
export type AdmissionStatus = Schemas["AdmissionStatus"];
export type AdmissionCreate = Schemas["AdmissionCreate"];
/** A patient the OPD has sent to the admission desk, with identity attached. */
export type AdmissionRequest = Schemas["AdmissionRequestRead"];
export type AdmissionRequestStatus = Schemas["AdmissionRequestStatus"];
export type DischargeType = Schemas["DischargeType"];
export type MedicationOrder = Schemas["MedicationOrderRead"];
export type MedicationOrderInput = Schemas["MedicationOrderCreate"];
export type MedicationRoute = Schemas["MedicationRoute"];
export type DrugSchedule = Schemas["DrugSchedule"];
/** One slot on the chart, with enough identity to give it safely. */
export type Dose = Schemas["DoseRead"];
export type DoseStatus = Schemas["DoseStatus"];
export type DischargeSummary = Schemas["SummaryRead"];
export type SummaryStatus = Schemas["SummaryStatus"];

// --- Clinical --------------------------------------------------------------
export type Encounter = Schemas["EncounterRead"];
export type EncounterSummary = Schemas["EncounterSummary"];
export type EncounterChart = Schemas["EncounterChart"];
export type EncounterStatus = Schemas["EncounterStatus"];
export type EncounterEvent = Schemas["EncounterEventRead"];
export type SafetyBanner = Schemas["SafetyBanner"];
export type PendingItems = Schemas["PendingItems"];
export type Vitals = Schemas["VitalsRead"];
export type VitalsInput = Schemas["VitalsCreate"];
export type ClinicalNote = Schemas["NoteRead"];
export type NoteInput = Schemas["NoteCreate"];
export type NoteType = Schemas["NoteType"];
export type NoteTemplate = Schemas["NoteTemplateRead"];
export type Diagnosis = Schemas["DiagnosisRead"];
export type DiagnosisInput = Schemas["DiagnosisCreate"];
export type Order = Schemas["OrderRead"];
export type OrderInput = Schemas["OrderCreate"];
export type OrderType = Schemas["OrderType"];
export type OrderPriority = Schemas["OrderPriority"];
export type CompleteConsultation = Schemas["CompleteConsultationRequest"];

// --- Diagnostics -----------------------------------------------------------
/** One open lab or radiology request, with the one thing it is waiting for. */
export type WorklistEntry = Schemas["WorklistEntry"];
export type WorklistStage = Schemas["WorklistStage"];
export type DiagnosticReport = Schemas["ReportRead"];
export type ReportSummary = Schemas["ReportSummary"];
export type ReportStatus = Schemas["ReportStatus"];
export type ResultValue = Schemas["ResultValueRead"];
export type ResultsSubmission = Schemas["ResultsSubmission"];
export type Analyte = Schemas["AnalyteRead"];
export type CatalogueItem = Schemas["CatalogueItemRead"];
export type Specimen = Schemas["SpecimenRead"];
export type SpecimenStatus = Schemas["SpecimenStatus"];

// --- Billing ---------------------------------------------------------------
/** One row of the cash counter's board: who owes what, and whether they are here. */
export type AccountBoardEntry = Schemas["AccountBoardEntry"];
export type VisitAccount = Schemas["VisitAccount"];
export type Invoice = Schemas["InvoiceRead"];
export type InvoiceSummary = Schemas["InvoiceSummary"];
export type InvoiceStatus = Schemas["InvoiceStatus"];
export type Charge = Schemas["ChargeRead"];
export type Payment = Schemas["PaymentRead"];
export type PaymentRequest = Schemas["PaymentRequest"];
export type PaymentMethod = Schemas["PaymentMethod"];

// --- Notifications ---------------------------------------------------------
/** One outbox row, carrying enough identity to find the person who complained. */
export type MessageSummary = Schemas["NotificationSummary"];
/** A message and every channel tried for it. */
export type Message = Schemas["NotificationRead"];
export type MessageAttempt = Schemas["AttemptRead"];
export type MessageStatus = Schemas["NotificationStatus"];
export type MessageCategory = Schemas["NotificationCategory"];
export type MessageChannel = Schemas["NotificationChannel"];
export type MessageTemplate = Schemas["MessageTemplateRead"];
export type MessageTemplateInput = Schemas["MessageTemplateCreate"];
export type MessageTemplateEdit = Schemas["MessageTemplateUpdate"];
export type TemplatePreview = Schemas["TemplatePreviewResult"];
export type Suppression = Schemas["SuppressionRead"];
export type SuppressionReason = Schemas["SuppressionReason"];

// --- Reporting -------------------------------------------------------------
/** Every section the caller's permissions allow, in one round trip. */
export type Dashboard = Schemas["Dashboard"];
export type FootfallReport = Schemas["FootfallReport"];
export type DailyCount = Schemas["DailyCount"];
export type DepartmentLoad = Schemas["DepartmentLoad"];
export type DoctorLoad = Schemas["DoctorLoad"];
/** Bed occupancy as management asks for it — distinct from `Occupancy`, which is the ward board's. */
export type OccupancyReport = Schemas["OccupancyReport"];
export type WardOccupancy = Schemas["WardOccupancy"];
export type InpatientReport = Schemas["InpatientReport"];
export type RevenueReport = Schemas["RevenueReport"];
export type CategoryRevenue = Schemas["CategoryRevenue"];
export type FollowUpCompliance = Schemas["FollowUpCompliance"];
export type QueueSnapshot = Schemas["QueueSnapshot"];
export type WaitingDoctor = Schemas["WaitingDoctor"];

/**
 * A paginated response.
 *
 * FastAPI's generic `Page[T]` becomes one flat schema per item type in
 * OpenAPI (`Page_PatientRead_`, `Page_EncounterSummary_`, …). Since every one
 * has the same shape, a single generic here is both accurate and far easier
 * to use than reaching for the right generated name at each call site.
 */
export type Page<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  /** True when another page follows this one — see `Page.has_more` server-side. */
  has_more: boolean;
};
