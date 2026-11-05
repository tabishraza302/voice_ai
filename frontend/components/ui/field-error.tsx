export function FieldError({ id, errors }: { id?: string; errors?: string[] }) {
  if (!errors?.length) return null;
  return (
    <ul id={id} className="mt-1.5 space-y-1 text-sm text-destructive" role="alert">
      {errors.map((error, index) => <li key={`${index}-${error}`}>{error}</li>)}
    </ul>
  );
}
