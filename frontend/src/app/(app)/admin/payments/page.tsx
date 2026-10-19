import { redirect } from "next/navigation";

/** The UPI ID now lives on the hospital profile, with the bill letterhead. */
export default function PaymentsAdminPage() {
  redirect("/admin/hospital");
}
