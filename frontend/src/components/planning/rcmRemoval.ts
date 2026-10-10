import type { ConfirmationOptions } from 'primevue/confirmationoptions'

import { plural } from '../../format'
import type { RcmRow } from '../../types'

/**
 * The prompt a row is removed behind, wherever it is removed from.
 *
 * The matrix's drawer and the row's own page both offer removal, and both must
 * say the same thing about what it costs: the tests are unlinked, not deleted,
 * and so are the findings. One definition is what keeps them from drifting.
 */
export function removeRcmRowPrompt(
  row: Pick<RcmRow, 'id' | 'process' | 'test_refs'>,
  accept: () => Promise<void>,
): ConfirmationOptions {
  const linked = row.test_refs?.length ?? 0
  return {
    header: 'Remove RCM row',
    message: `Remove "${row.process?.trim() || row.id}"?`
      + (linked
        ? ` Its ${plural(linked, 'linked test')} will be unlinked, not deleted; findings will be unlinked too.`
        : ' Any linked findings will be unlinked.'),
    icon: 'aw-icon aw-icon-triangle-alert',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept,
  }
}
