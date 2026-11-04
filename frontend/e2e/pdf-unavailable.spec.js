import { expect, test } from '@playwright/test'

const TENANT_ID = '11111111-1111-4111-8111-111111111111'
const PATENT_ID = '22222222-2222-4222-8222-222222222222'
const VERSION_ID = '33333333-3333-4333-8333-333333333333'

const patent = {
  id: PATENT_ID,
  title: 'Fixture patent — PDF storage unavailable',
  abstract: 'A non-sensitive fixture used to exercise the unavailable-document workflow.',
  publication_number: 'US-TEST-2026-000001-A1',
  application_number: 'US-TEST-2026-000001',
  jurisdiction: 'US',
  source_type: 'UPLOAD',
  status: 'INDEXED',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  versions: [{
    id: VERSION_ID,
    version_number: 1,
    mime_type: 'application/pdf',
    page_count: 1,
    extraction_status: 'EXTRACTED',
    extraction_queued_at: null,
    extraction_started_at: null,
    extraction_error_code: null,
    text_indexing_status: 'COMPLETED',
    text_indexing_attempts: 1,
    text_indexing_error_code: null,
    schematic_indexing_status: 'COMPLETED',
    schematic_indexing_attempts: 1,
    schematic_indexing_error_code: null,
    created_at: '2026-01-01T00:00:00Z',
    file_url: `http://127.0.0.1:4173/api/v1/patents/${PATENT_ID}/versions/${VERSION_ID}/file/`,
  }],
}

function jsonResponse(route, payload, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload),
  })
}

test('shows Document Unavailable when the patent PDF API returns HTTP 404', async ({ page }) => {
  let pdfRequestObserved = false

  await page.route('**/api/v1/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname

    if (pathname.endsWith('/auth/csrf/')) {
      await jsonResponse(route, { csrfToken: 'playwright-csrf-token' })
      return
    }
    if (pathname.endsWith('/auth/me/')) {
      await jsonResponse(route, {
        user: {
          id: '44444444-4444-4444-8444-444444444444',
          username: 'e2e-reviewer',
          email: 'reviewer@example.test',
          full_name: 'E2E Patent Reviewer',
          is_staff: false,
        },
      })
      return
    }
    if (pathname.endsWith('/tenants/')) {
      await jsonResponse(route, {
        results: [{
          id: TENANT_ID,
          name: 'E2E Patent Workspace',
          slug: 'e2e-patent-workspace',
          role: 'OWNER',
          membership_status: 'ACTIVE',
        }],
      })
      return
    }
    if (pathname === '/api/v1/patents/') {
      await jsonResponse(route, { results: [patent] })
      return
    }
    if (pathname === `/api/v1/patents/${PATENT_ID}/versions/${VERSION_ID}/file/`) {
      pdfRequestObserved = true
      await jsonResponse(route, { error: 'PDF file not found on storage server.' }, 404)
      return
    }
    if (pathname.endsWith('/claims/')) {
      await jsonResponse(route, { results: [] })
      return
    }
    if (pathname === '/api/v1/analyses/' || pathname.startsWith('/api/v1/analyses/')) {
      await jsonResponse(route, { results: [] })
      return
    }

    // Unused read-only list endpoints return empty collections. This keeps the
    // test independent of backend data while still requiring the UI to use its
    // real session, tenant, patent, and PDF URL flows.
    await jsonResponse(route, { results: [] })
  })

  await page.goto(`/patents/${PATENT_ID}`)

  await expect(page.getByRole('heading', { name: 'Patent details' })).toBeVisible()
  await expect(page.getByText(patent.title)).toBeVisible()
  await expect(page.getByTestId('pdf-unavailable')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByRole('heading', { name: 'Document Unavailable' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry document' })).toBeVisible()
  expect(pdfRequestObserved).toBe(true)
})
