// Shared fail-closed action validation for the local synthetic fixture only.
export function texts(result) {
  return (result.content || []).filter(block => block.type === 'text').map(block => block.text).join('\n');
}

export function actionCode(action, observation, approvedValues) {
  if (action.action === 'stop') throw new Error(`GLM stopped: ${String(action.reason).slice(0, 200)}`);
  if (!['click', 'fill'].includes(action.action)) throw new Error('Only click/fill allowed');
  if (!Number.isSafeInteger(action.index) || action.index < 0) throw new Error('Invalid AX index');
  const line = texts(observation).split('\n').find(line => new RegExp(`^\\s*${action.index}\\s`).test(line));
  if (!line) throw new Error('AX index not in current observation');
  if (!/Full name|\bEmail\b|I confirm this is synthetic test data|Continue to experience|Describe a relevant project|Submit synthetic application/.test(line)) {
    throw new Error('Target is not an approved synthetic form control');
  }
  if (!/button|check box|checkbox|entry|text field|text input|text area|editable|textbox/i.test(line)) {
    throw new Error(`Unsupported target role: ${line.slice(0, 160)}`);
  }
  if (/close|menu|address|location|back|reload|bookmark|permission|password/i.test(line)) throw new Error('Target outside fixture scope');
  const click = `await app.click(${action.index});`;
  if (action.action === 'click') return `${click} await app.getAXState({disableDiffing:true});`;
  if (!approvedValues.includes(action.value)) throw new Error('Unapproved fixture value');
  if (!/entry|text field|text input|text area|editable|textbox/i.test(line)) throw new Error('Fill requires editable text target');
  return `${click} await app.pressKey("ctrl+a"); await app.typeText(${JSON.stringify(action.value)}); await app.getAXState({disableDiffing:true});`;
}
